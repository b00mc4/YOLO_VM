from __future__ import annotations
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fastapi import BackgroundTasks, HTTPException, Request, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_client_ip
from app.core.config import get_settings
from app.core.security import (
    _DUMMY_PASSWORD_HASH as hash_security_dummy,
    create_access_token,
    generate_secure_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.verify import Verify, VerifyType
from app.services import audit_service, email_service, login_security_service
from app.models.group import Group
from app.models.user import User, UserRole
from app.core.rate_limit import get_rate_limiter, password_reauth_key, PASSWORD_REAUTH_LIMIT, PASSWORD_REAUTH_WINDOW_SECONDS
from app.core.account_lockout import AccountLocked, get_account_locker
from app.core.error_messages import Auth, UserErrors
from app.core.session_manager import RotatedRefreshToken, session_manager
from app.core.stream_registry import stream_registry
from app.schemas.auth import ActiveSessionsResponse, SessionInfo
from app.core.background import spawn_background
from app.core.rate_limit import InMemorySingleWorkerRateLimiter, RateLimitExceeded
from app.core.alert_cooldown import InMemorySingleWorkerCooldown

_rapid_login_limiter = InMemorySingleWorkerRateLimiter()
_rapid_login_alert_cooldown = InMemorySingleWorkerCooldown()

_RAPID_LOGIN_LIMIT = 5
_RAPID_LOGIN_WINDOW_SECONDS = 60

settings = get_settings()

_LOGIN_USERNAME_LIMIT = 50
_LOGIN_USERNAME_WINDOW_SECONDS = 30 * 60

_VERIFY_TOKEN_TTL: dict[VerifyType, timedelta] = {
    VerifyType.PASSWORD_RESET: timedelta(minutes=15),
    VerifyType.INITIAL_SETUP: timedelta(days=1),
    VerifyType.EMAIL_CHANGE: timedelta(hours=12),
}

_SET_PASSWORD_ELIGIBLE_TYPES = (VerifyType.INITIAL_SETUP, VerifyType.PASSWORD_RESET)


@dataclass(frozen=True, slots=True)
class RefreshedTokens:
    access_token: str
    refresh_token: str | None
    remember_me: bool

async def authenticate_user(db: AsyncSession, request: Request, username: str, password: str, remember_me: bool):
    normalized_username = username.strip().lower()
    rate_limit_key = f"login:username:{normalized_username}"
    locker_key = normalized_username

    try:
        get_account_locker().check_locked(locker_key)
    except AccountLocked:
        await audit_service.log_action(
            db,
            request,
            action="login_blocked_locked",
            detail=f"login attempt blocked, account locked for username: {username}",
        )
        await db.commit()
        raise

    get_rate_limiter().check(rate_limit_key, _LOGIN_USERNAME_LIMIT, _LOGIN_USERNAME_WINDOW_SECONDS)

    result = await db.execute(
        select(User, Group.is_active.label("village_is_active"))
        .outerjoin(Group, User.village_id == Group.id)
        .where(User.username == normalized_username)
    )
    row = result.one_or_none()
    
    if row is not None:
        user, village_is_active = row
        if user.role == UserRole.SUPERADMIN:
            village_is_active = True
    else:
        user = None
        village_is_active = True

    hash_to_check = (
        user.hashpassword
        if (user is not None and user.hashpassword is not None)
        else hash_security_dummy
    )
    password_ok = await verify_password(password, hash_to_check)

    credential_failed = (
    user is None
    or user.hashpassword is None
    or not password_ok
    )

    login_failed = (
    credential_failed
    or not user.is_active
    or not user.is_verify
    or not village_is_active
    )

    if login_failed:
        locked_for_seconds = None
        if credential_failed:
            locked_for_seconds = get_account_locker().register_failure(locker_key)

        await audit_service.log_action(
            db,
            request,
            action="login_failed",
            detail=(
                f"unknown username: {username}"
                if user is None
                else f"failed login attempt for username: {username}"
            ),
            user_id=user.id if user is not None else None,
            village_id=user.village_id if user is not None else None,
        )

        if locked_for_seconds is not None:
            await login_security_service.record_bruteforce_audit(
                db, request, username, user, locked_for_seconds
            )

        await db.commit()

        if locked_for_seconds is not None:
            try:
                await login_security_service.publish_bruteforce_alert(
                    username, user, locked_for_seconds, get_client_ip(request)
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to publish bruteforce alert: {e}")
            raise AccountLocked(retry_after_seconds=locked_for_seconds)

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.INVALID_CREDENTIALS)

    get_account_locker().reset(locker_key)
    await audit_service.log_action(
        db,
        request,
        action="login_success",
        detail=f"successful login for username: {username} (remember_me={remember_me})",
        user_id=user.id,
        village_id=user.village_id,
    )
    await db.commit()
    get_rate_limiter().reset(rate_limit_key)

    try:
        _rapid_login_limiter.check(f"rapid_login:{user.id}", _RAPID_LOGIN_LIMIT, _RAPID_LOGIN_WINDOW_SECONDS)
    except RateLimitExceeded:
        if _rapid_login_alert_cooldown.allow(f"rapid_alert:{user.id}", _RAPID_LOGIN_WINDOW_SECONDS):
            spawn_background(
                login_security_service.publish_rapid_login_alert(
                    user, get_client_ip(request), _RAPID_LOGIN_LIMIT, _RAPID_LOGIN_WINDOW_SECONDS
                )
            )

    return user

def _refresh_token_lifetime(remember_me: bool) -> timedelta:
    if remember_me:
        return timedelta(days=settings.refresh_token_expire_days)
    return timedelta(hours=settings.refresh_token_session_expire_hours)


def _invalid_refresh_token_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.INVALID_OR_EXPIRED_REFRESH_TOKEN)


async def _load_refreshable_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    result = await db.execute(
        select(User, Group.is_active.label("village_is_active"))
        .outerjoin(Group, User.village_id == Group.id)
        .where(User.id == user_id)
    )
    row = result.one_or_none()
    if row is None:
        raise _invalid_refresh_token_error()

    user, village_is_active = row
    if not user.is_active or not user.is_verify:
        raise _invalid_refresh_token_error()
    if user.role != UserRole.SUPERADMIN and not village_is_active:
        raise _invalid_refresh_token_error()
    return user


async def _delete_sessions(db: AsyncSession, session_ids: Sequence[uuid.UUID]) -> None:
    await db.execute(delete(RefreshToken).where(RefreshToken.id.in_(session_ids)))


async def _get_user_village_id(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    return await db.scalar(select(User.village_id).where(User.id == user_id))


async def issue_tokens(db: AsyncSession, request: Request, user: User, remember_me: bool) -> tuple[str, str]:
    session_id = uuid.uuid4()
    raw_refresh_token = generate_secure_token()
    db.add(
        RefreshToken(
            id=session_id,
            user_id=user.id,
            token_hash=hash_token(raw_refresh_token),
            expire_at=datetime.now(timezone.utc) + _refresh_token_lifetime(remember_me),
            remember_me=remember_me,
        )
    )
    await db.commit()

    evicted_session_ids = session_manager.add_session(user.id, session_id)
    if evicted_session_ids:
        stream_registry.revoke_sessions(evicted_session_ids)
        await _delete_sessions(db, evicted_session_ids)
        await audit_service.log_action(
            db,
            request,
            action="session_evicted",
            detail=(
                f"signed out {len(evicted_session_ids)} oldest session(s): "
                f"exceeded {session_manager.max_sessions} concurrent sessions"
            ),
            user_id=user.id,
            village_id=user.village_id,
        )
        await db.commit()

    return create_access_token(user.id, session_id), raw_refresh_token


async def rotate_refresh_token(db: AsyncSession, request: Request, raw_refresh_token: str) -> RefreshedTokens:
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
    )
    stored_token = result.scalar_one_or_none()

    if stored_token is None:
        return await _resolve_rotated_refresh_token(db, request, token_hash)

    session_id = stored_token.id
    is_expired = stored_token.expire_at < datetime.now(timezone.utc)
    if is_expired or not session_manager.is_valid_session(stored_token.user_id, session_id):
        session_manager.remove_session(session_id)
        stream_registry.revoke_sessions([session_id])
        await db.delete(stored_token)
        await db.commit()
        raise _invalid_refresh_token_error()

    user = await _load_refreshable_user(db, stored_token.user_id)

    new_raw_refresh_token = generate_secure_token()
    session_manager.record_rotation(token_hash, user.id, session_id)
    stored_token.token_hash = hash_token(new_raw_refresh_token)
    stored_token.expire_at = datetime.now(timezone.utc) + _refresh_token_lifetime(stored_token.remember_me)
    await db.commit()

    return RefreshedTokens(
        access_token=create_access_token(user.id, session_id),
        refresh_token=new_raw_refresh_token,
        remember_me=stored_token.remember_me,
    )


async def _resolve_rotated_refresh_token(db: AsyncSession, request: Request, token_hash: str) -> RefreshedTokens:
    rotation = session_manager.find_rotation(token_hash)
    if rotation is None:
        raise _invalid_refresh_token_error()

    if not rotation.is_within_grace(settings.refresh_reuse_grace_seconds):
        await _revoke_session_on_reuse(db, request, rotation)
        raise _invalid_refresh_token_error()

    remember_me = await db.scalar(select(RefreshToken.remember_me).where(RefreshToken.id == rotation.session_id))
    if remember_me is None or not session_manager.is_valid_session(rotation.user_id, rotation.session_id):
        raise _invalid_refresh_token_error()

    user = await _load_refreshable_user(db, rotation.user_id)
    return RefreshedTokens(
        access_token=create_access_token(user.id, rotation.session_id),
        refresh_token=None,
        remember_me=remember_me,
    )


async def _revoke_session_on_reuse(db: AsyncSession, request: Request, rotation: RotatedRefreshToken) -> None:
    session_manager.remove_session(rotation.session_id)
    stream_registry.revoke_sessions([rotation.session_id])
    await _delete_sessions(db, [rotation.session_id])
    await audit_service.log_action(
        db,
        request,
        action="refresh_token_reuse_detected",
        detail="rotated refresh token reused after grace period, session revoked",
        user_id=rotation.user_id,
        village_id=await _get_user_village_id(db, rotation.user_id),
    )
    await db.commit()


async def revoke_refresh_token(db: AsyncSession, request: Request, raw_refresh_token: str) -> None:
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken.id, RefreshToken.user_id).where(RefreshToken.token_hash == token_hash)
    )
    row = result.one_or_none()

    if row is not None:
        session_id, user_id = row
    else:
        rotation = session_manager.find_rotation(token_hash)
        if rotation is None:
            return
        session_id, user_id = rotation.session_id, rotation.user_id

    session_manager.remove_session(session_id)
    stream_registry.revoke_sessions([session_id])
    await _delete_sessions(db, [session_id])
    await audit_service.log_action(
        db,
        request,
        action="logout",
        detail="user logged out and refresh token revoked",
        user_id=user_id,
        village_id=await _get_user_village_id(db, user_id),
    )
    await db.commit()


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    stream_registry.revoke_sessions(session_manager.remove_all_sessions(user_id))
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))


async def change_password(
    db: AsyncSession,
    request: Request,
    current_user: User,
    current_password: str,
    new_password: str,
) -> None:
    reauth_key = password_reauth_key(current_user.id)
    get_rate_limiter().check(reauth_key, PASSWORD_REAUTH_LIMIT, PASSWORD_REAUTH_WINDOW_SECONDS)

    if current_user.hashpassword is None or not await verify_password(current_password, current_user.hashpassword):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.CURRENT_PASSWORD_INCORRECT)

    get_rate_limiter().reset(reauth_key)

    current_user.hashpassword = await hash_password(new_password)
    current_user.password_changed_at = datetime.now(timezone.utc)

    await revoke_all_refresh_tokens(db, current_user.id)

    await audit_service.log_action(
        db,
        request,
        action="change_password",
        detail="password changed",
        user_id=current_user.id,
        village_id=current_user.village_id,
    )
    await db.commit()


async def create_verify_token(
    db: AsyncSession, user: User, verify_type: VerifyType, new_email: str | None = None
) -> str:
    raw_token = generate_secure_token()
    verify_entry = Verify(
        user_id=user.id,
        type=verify_type,
        new_email=new_email,
        token_hash=hash_token(raw_token),
        expire_at=datetime.now(timezone.utc) + _VERIFY_TOKEN_TTL[verify_type],
    )
    db.add(verify_entry)
    await db.flush()
    return raw_token


async def invalidate_pending_verify_tokens(
    db: AsyncSession,
    user_id: uuid.UUID,
    verify_type: VerifyType,
    exclude_token_hash: str | None = None,
) -> None:
    stmt = (
        update(Verify)
        .where(Verify.user_id == user_id, Verify.type == verify_type, Verify.used.is_(False))
        .values(used=True)
    )
    if exclude_token_hash is not None:
        stmt = stmt.where(Verify.token_hash != exclude_token_hash)
    await db.execute(stmt)


async def request_password_reset(
    db: AsyncSession, background_tasks: BackgroundTasks, email: str
) -> None:
    normalized_email = email.strip().lower()

    if email_service.is_email_service_degraded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ระบบส่งอีเมลขัดข้องชั่วคราว กรุณาลองใหม่ภายหลัง",
        )

    result = await db.execute(select(User).where(User.email == normalized_email))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        return

    raw_token = await create_verify_token(db, user, VerifyType.PASSWORD_RESET)
    await invalidate_pending_verify_tokens(
        db, user.id, VerifyType.PASSWORD_RESET, exclude_token_hash=hash_token(raw_token)
    )
    await audit_service.log_action(
        db,
        request=None,
        action="password_reset_requested",
        detail=f"password reset requested for email: {email}",
        user_id=user.id,
        village_id=user.village_id,
    )
    await db.commit()
    background_tasks.add_task(
        email_service.send_set_password_email_background, user.email, raw_token
    )


async def _resolve_set_password_token(db: AsyncSession, raw_token: str) -> Verify:
    token_hash = hash_token(raw_token)
    result = await db.execute(
        select(Verify).where(
            Verify.token_hash == token_hash,
            Verify.used.is_(False),
            Verify.type.in_(_SET_PASSWORD_ELIGIBLE_TYPES),
        )
    )
    verify_entry = result.scalar_one_or_none()
    if verify_entry is None or verify_entry.expire_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.INVALID_OR_EXPIRED_TOKEN)
    return verify_entry


async def verify_set_password_token(db: AsyncSession, raw_token: str) -> None:
    await _resolve_set_password_token(db, raw_token)


async def set_password(db: AsyncSession, raw_token: str, new_password: str) -> str:
    verify_entry = await _resolve_set_password_token(db, raw_token)
    result = await db.execute(select(User).where(User.id == verify_entry.user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.INVALID_OR_EXPIRED_TOKEN)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.ACCOUNT_INACTIVE)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.ACCOUNT_INACTIVE)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.ACCOUNT_INACTIVE)

    user.hashpassword = await hash_password(new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    user.is_verify = True
    verify_entry.used = True
    token_hash = hash_token(raw_token)

    await invalidate_pending_verify_tokens(
        db, user.id, verify_entry.type, exclude_token_hash=token_hash
    )
    await revoke_all_refresh_tokens(db, user.id)
    await audit_service.log_action(
        db,
        request=None,
        action="password_set",
        detail=f"password successfully set for username: {user.username}",
        user_id=user.id,
        village_id=user.village_id,
    )
    await db.commit()

    return user.username

async def confirm_email_change(db: AsyncSession, request: Request, raw_token: str) -> tuple[str, str]:
    token_hash = hash_token(raw_token)
    result = await db.execute(
        select(Verify).where(
            Verify.token_hash == token_hash,
            Verify.type == VerifyType.EMAIL_CHANGE,
            Verify.used.is_(False),
        )
    )
    verify_entry = result.scalar_one_or_none()

    if verify_entry is None or verify_entry.expire_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.INVALID_OR_EXPIRED_TOKEN)

    result = await db.execute(select(User).where(User.id == verify_entry.user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.INVALID_OR_EXPIRED_TOKEN)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.ACCOUNT_INACTIVE)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=Auth.ACCOUNT_INACTIVE)

    existing_email_result = await db.execute(
        select(User.id).where(User.email == verify_entry.new_email, User.id != user.id)
    )
    if existing_email_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=UserErrors.EMAIL_ALREADY_IN_USE)

    old_email = user.email
    user.email = verify_entry.new_email
    verify_entry.used = True

    await invalidate_pending_verify_tokens(db, user.id, VerifyType.EMAIL_CHANGE, exclude_token_hash=token_hash)

    await audit_service.log_action(
        db,
        request,
        action="email_changed",
        detail=f"email changed from {old_email} to {user.email} for username: {user.username}",
        user_id=user.id,
        village_id=user.village_id,
    )

    await db.commit()

    return user.username, user.email

async def get_active_sessions(db: AsyncSession, request: Request, current_user: User) -> ActiveSessionsResponse:
    current_session_id = getattr(request.state, "session_id", None)
    result = await db.execute(
        select(RefreshToken)
        .where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.expire_at > datetime.now(timezone.utc),
        )
        .order_by(RefreshToken.created_at.desc())
    )

    sessions = [
        SessionInfo(
            id=token.id,
            created_at=token.created_at,
            expire_at=token.expire_at,
            is_current=token.id == current_session_id,
        )
        for token in result.scalars().all()
        if session_manager.is_valid_session(current_user.id, token.id)
    ]

    return ActiveSessionsResponse(
        active_sessions_count=len(sessions),
        max_sessions=session_manager.max_sessions,
        sessions=sessions,
    )


async def cleanup_expired_refresh_tokens(db: AsyncSession) -> int:
    result = await db.execute(delete(RefreshToken).where(RefreshToken.expire_at < datetime.now(timezone.utc)))
    await db.commit()
    return result.rowcount


async def restore_active_sessions(db: AsyncSession) -> int:
    result = await db.execute(
        select(RefreshToken.id, RefreshToken.user_id)
        .where(RefreshToken.expire_at > datetime.now(timezone.utc))
        .order_by(RefreshToken.created_at.asc())
    )
    rows = result.all()

    evicted_session_ids: list[uuid.UUID] = []
    for session_id, user_id in rows:
        evicted_session_ids.extend(session_manager.add_session(user_id, session_id))

    if evicted_session_ids:
        await _delete_sessions(db, evicted_session_ids)
        await db.commit()

    return len(rows) - len(evicted_session_ids)
