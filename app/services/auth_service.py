from __future__ import annotations
import uuid
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
from app.core.session_manager import session_manager
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

async def issue_tokens(db: AsyncSession, user: User, remember_me: bool):
    raw_refresh_token = generate_secure_token()
    token_hash = hash_token(raw_refresh_token)

    if remember_me:
        expires_delta = timedelta(days=settings.refresh_token_expire_days)
    else:
        expires_delta = timedelta(hours=settings.refresh_token_session_expire_hours)

    refresh_token = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expire_at=datetime.now(timezone.utc) + expires_delta,
        remember_me=remember_me,
    )
    db.add(refresh_token)
    await db.commit()

    session_manager.add_session(user.id, token_hash)
    access_token = create_access_token(user.id, token_hash)

    return access_token, raw_refresh_token


async def rotate_refresh_token(db: AsyncSession, raw_refresh_token: str):
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
    )
    stored_token = result.scalar_one_or_none()

    if stored_token is None or stored_token.expire_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.INVALID_OR_EXPIRED_REFRESH_TOKEN)

    result = await db.execute(
        select(User, Group.is_active.label("village_is_active"))
        .outerjoin(Group, User.village_id == Group.id)
        .where(User.id == stored_token.user_id)
    )
    row = result.one_or_none()
    if row is None:
        user, village_is_active = None, False
    else:
        user, village_is_active = row

    if user is None or not user.is_active or not user.is_verify:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.INVALID_OR_EXPIRED_REFRESH_TOKEN)

    # Check if this session was kicked out from memory (e.g. max sessions reached)
    if not session_manager.is_valid_session(user.id, token_hash):
        await db.delete(stored_token)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.INVALID_OR_EXPIRED_REFRESH_TOKEN)

    if user.role != UserRole.SUPERADMIN and not village_is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.INVALID_OR_EXPIRED_REFRESH_TOKEN)

    remember_me = stored_token.remember_me
    await db.delete(stored_token)
    session_manager.remove_session_with_grace(token_hash)
    access_token, new_raw_refresh_token = await issue_tokens(db, user, remember_me)
    return access_token, new_raw_refresh_token, remember_me

async def revoke_refresh_token(db: AsyncSession, raw_refresh_token: str):
    token_hash = hash_token(raw_refresh_token)
    
    result = await db.execute(select(RefreshToken.user_id).where(RefreshToken.token_hash == token_hash))
    user_id = result.scalar_one_or_none()
    
    village_id = None
    if user_id:
        user_res = await db.execute(select(User.village_id).where(User.id == user_id))
        village_id = user_res.scalar_one_or_none()

    session_manager.remove_session_by_id(token_hash)
    await db.execute(delete(RefreshToken).where(RefreshToken.token_hash == token_hash))
    await audit_service.log_action(
        db,
        request=None,
        action="logout",
        detail="user logged out and refresh token revoked",
        user_id=user_id,
        village_id=village_id,
    )
    await db.commit()

async def revoke_all_refresh_tokens(db: AsyncSession, user_id: uuid.UUID):
    session_manager.remove_all_sessions(user_id)
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
    await db.commit()
    await db.commit()
    

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

from app.schemas.auth import ActiveSessionsResponse, SessionInfo

async def get_active_sessions(db: AsyncSession, request: Request, current_user: User) -> ActiveSessionsResponse:
    # 1. Fetch unexpired refresh tokens from DB
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.expire_at > datetime.now(timezone.utc)
        ).order_by(RefreshToken.created_at.desc())
    )
    tokens = result.scalars().all()
    
    # 2. Filter with session_manager to get only TRULY active sessions (max 5)
    current_raw_token = request.cookies.get("refresh_token")
    current_token_hash = hash_token(current_raw_token) if current_raw_token else None

    active_sessions = []
    for token in tokens:
        if session_manager.is_valid_session(current_user.id, token.token_hash):
            active_sessions.append(
                SessionInfo(
                    id=token.id,
                    created_at=token.created_at,
                    expire_at=token.expire_at,
                    is_current=(token.token_hash == current_token_hash)
                )
            )

    return ActiveSessionsResponse(
        active_sessions_count=len(active_sessions),
        max_sessions=session_manager.max_sessions,
        sessions=active_sessions
    )

async def cleanup_expired_refresh_tokens(db: AsyncSession) -> int:
    stmt = delete(RefreshToken).where(RefreshToken.expire_at < datetime.now(timezone.utc))
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount

async def restore_active_sessions(db: AsyncSession) -> int:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count

async def restore_active_sessions(db: AsyncSession) -> None:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count

from app.schemas.auth import ActiveSessionsResponse, SessionInfo

async def get_active_sessions(db: AsyncSession, request: Request, current_user: User) -> ActiveSessionsResponse:
    # 1. Fetch unexpired refresh tokens from DB
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.expire_at > datetime.now(timezone.utc)
        ).order_by(RefreshToken.created_at.desc())
    )
    tokens = result.scalars().all()
    
    # 2. Filter with session_manager to get only TRULY active sessions (max 5)
    current_raw_token = request.cookies.get("refresh_token")
    current_token_hash = hash_token(current_raw_token) if current_raw_token else None

    active_sessions = []
    for token in tokens:
        if session_manager.is_valid_session(current_user.id, token.token_hash):
            active_sessions.append(
                SessionInfo(
                    id=token.id,
                    created_at=token.created_at,
                    expire_at=token.expire_at,
                    is_current=(token.token_hash == current_token_hash)
                )
            )

    return ActiveSessionsResponse(
        active_sessions_count=len(active_sessions),
        max_sessions=session_manager.max_sessions,
        sessions=active_sessions
    )

async def cleanup_expired_refresh_tokens(db: AsyncSession) -> int:
    stmt = delete(RefreshToken).where(RefreshToken.expire_at < datetime.now(timezone.utc))
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount

async def restore_active_sessions(db: AsyncSession) -> int:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count

async def restore_active_sessions(db: AsyncSession) -> None:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count

async def cleanup_expired_refresh_tokens(db: AsyncSession) -> int:
    stmt = delete(RefreshToken).where(RefreshToken.expire_at < datetime.now(timezone.utc))
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount

async def restore_active_sessions(db: AsyncSession) -> int:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count

async def restore_active_sessions(db: AsyncSession) -> None:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count

async def restore_active_sessions(db: AsyncSession) -> int:
    stmt = select(RefreshToken).where(RefreshToken.expire_at > datetime.now(timezone.utc)).order_by(RefreshToken.created_at.asc())
    result = await db.execute(stmt)
    tokens = result.scalars().all()
    count = 0
    for token in tokens:
        session_manager.add_session(token.user_id, token.token_hash)
        count += 1
    return count