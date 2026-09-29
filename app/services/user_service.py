from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import BackgroundTasks, HTTPException, Request, UploadFile, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import verify_village_scope
from app.core.account_lockout import get_account_locker
from app.core.security import hash_password, hash_token
from app.models.contact import Contact
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.models.verify import Verify, VerifyType
from app.schemas.common import PaginatedResponse
from app.schemas.contact import ContactRead
from app.schemas.user import (
    AdminResetPasswordRequest,
    EmailChangeRequest,
    LockedAccountEntry,
    UserCreate,
    UserDetail,
    UserFullnameUpdate,
    UserMeDetail,
    UserProfileRead,
    UserStatusUpdate,
    UserSummary,
    UserRegister
)
from app.services import audit_service, auth_service, email_service, storage_service, village_service
from app.models.group import Group
from app.core.error_messages import Auth, AvatarErrors, Common, UserErrors
from app.core.db_utils import escape_like
from app.core.rate_limit import get_rate_limiter

_RESEND_INVITE_COOLDOWN = timedelta(minutes=1)
_EMAIL_CHANGE_COOLDOWN = timedelta(minutes=1)
from app.core.config import get_settings
settings = get_settings()

_AVATAR_UPLOAD_LIMIT = 10
_AVATAR_UPLOAD_WINDOW_SECONDS = 30 * 60


def _build_avatar_url(request: Request, user: User) -> str | None:
    if user.avatar_path is None:
        return None
    return str(request.url_for("get_user_avatar", user_id=user.id))


async def _get_user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UserErrors.NOT_FOUND)
    return user


def _verify_user_write_scope(current_user: User, target: User) -> None:
    if current_user.role == UserRole.SUPERADMIN:
        return
    if target.id == current_user.id:
        return
    if current_user.role == UserRole.ADMIN:
        if target.role != UserRole.USER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=UserErrors.SCOPE_ADMIN_ONLY_USER,
            )
        if target.village_id != current_user.village_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=UserErrors.SCOPE_OUTSIDE_VILLAGE,
            )
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=Common.INSUFFICIENT_PERMISSIONS)


def _verify_avatar_view_scope(current_user: User, target: User) -> None:
    if current_user.role == UserRole.SUPERADMIN:
        return
    if target.id == current_user.id:
        return
    if current_user.role == UserRole.ADMIN and target.village_id == current_user.village_id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=Common.INSUFFICIENT_PERMISSIONS)


def _verify_password_reset_scope(current_user: User, target: User) -> None:
    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UserErrors.CANNOT_RESET_OWN_PASSWORD,
        )
    if current_user.role == UserRole.SUPERADMIN:
        if target.role == UserRole.SUPERADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=UserErrors.CANNOT_RESET_SUPERADMIN_PASSWORD,
            )
        return
    if current_user.role == UserRole.ADMIN:
        if target.role != UserRole.USER or target.village_id != current_user.village_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=UserErrors.SCOPE_PASSWORD_RESET_DENIED,
            )
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=Common.INSUFFICIENT_PERMISSIONS)

def _build_user_list_village_scope_filters(
    current_user: User, village_id_filter: uuid.UUID | None
) -> list:
    if current_user.role == UserRole.SUPERADMIN:
        if village_id_filter is not None:
            return [User.village_id == village_id_filter]
        return []

    if village_id_filter is not None and village_id_filter != current_user.village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
        )
    return [User.village_id == current_user.village_id]

def _build_user_list_filters(
    current_user: User,
    village_id_filter: uuid.UUID | None,
    role_filter: UserRole | None,
    is_active_filter: bool | None,
    search: str | None,
) -> list:
    filters = _build_user_list_village_scope_filters(current_user, village_id_filter)

    if role_filter is not None:
        filters.append(User.role == role_filter)
    if is_active_filter is not None:
        filters.append(User.is_active == is_active_filter)
    if search:
        pattern = f"%{escape_like(search)}%"
        filters.append(or_(User.fullname.ilike(pattern, escape="\\"), User.username.ilike(pattern, escape="\\")))

    return filters


async def _to_user_detail(db: AsyncSession, request: Request, user: User) -> UserDetail:
    contact_count_result = await db.execute(
        select(func.count()).select_from(Contact).where(Contact.user_id == user.id)
    )
    contact_count = contact_count_result.scalar_one()

    village_name = None
    if user.village_id:
        village_result = await db.execute(select(Group.name).where(Group.id == user.village_id))
        village_name = village_result.scalar_one_or_none()

    return UserDetail(
        id=user.id,
        username=user.username,
        fullname=user.fullname,
        email=user.email,
        role=user.role,
        village_id=user.village_id,
        village_name=village_name,
        is_active=user.is_active,
        is_verify=user.is_verify,
        created_at=user.created_at,
        contact_count=contact_count,
        avatar_url=_build_avatar_url(request, user),
    )


async def _to_user_me_detail(db: AsyncSession, request: Request, user: User) -> UserMeDetail:
    contacts_result = await db.execute(
        select(Contact).where(Contact.user_id == user.id).order_by(Contact.created_at.desc())
    )
    contacts = contacts_result.scalars().all()

    return UserMeDetail(
        id=user.id,
        username=user.username,
        fullname=user.fullname,
        email=user.email,
        role=user.role,
        village_id=user.village_id,
        is_active=user.is_active,
        is_verify=user.is_verify,
        created_at=user.created_at,
        contacts=[ContactRead.model_validate(contact) for contact in contacts],
        avatar_url=_build_avatar_url(request, user),
    )

def _to_user_register(user: User) -> UserRegister:
    return UserRegister(
        id=user.id,
        username=user.username,
        role=user.role,
        village_id=user.village_id,
        created_at=user.created_at,
    )


async def create_user(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    payload: UserCreate,
) -> UserRegister:
    if email_service.is_email_service_degraded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ไม่สามารถสร้างผู้ใช้ได้ในขณะนี้เนื่องจากระบบอีเมลขัดข้อง",
        )

    if current_user.role == UserRole.ADMIN:
        if payload.role != UserRole.USER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=UserErrors.CANNOT_CREATE_ADMIN_OR_SUPERADMIN,
            )
        village_id = current_user.village_id
    elif current_user.role == UserRole.SUPERADMIN:
        village_id = payload.village_id
        if payload.role != UserRole.SUPERADMIN:
            village = await village_service.get_village(db, village_id)
            if not village.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=Auth.VILLAGE_INACTIVE,
                )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Role not authorized to create users.",
        )

    if payload.role == UserRole.USER:
        existing_admin_count_result = await db.execute(
            select(func.count()).select_from(User).where(
                User.village_id == village_id,
                User.role == UserRole.ADMIN
            )
        )
        if existing_admin_count_result.scalar_one() == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UserErrors.VILLAGE_NEEDS_ADMIN_FIRST,
            )

    user = User(
        username=payload.username,
        fullname=payload.fullname,
        email=payload.email,
        role=payload.role,
        village_id=village_id,
        hashpassword=None,
        is_active=True,
        is_verify=False,
    )
    db.add(user)
    await db.flush()

    await audit_service.log_action(
        db,
        request,
        action="user_created",
        detail=f"created user: {user.username} (role={user.role.value})",
        user_id=current_user.id,
        village_id=village_id,
    )

    raw_token = await auth_service.create_verify_token(db, user, VerifyType.INITIAL_SETUP)

    await db.commit()
    await db.refresh(user)

    background_tasks.add_task(email_service.send_invite_email_background, user.email, raw_token)

    return _to_user_register(user)


async def list_users(
    db: AsyncSession,
    request: Request,
    current_user: User,
    village_id_filter: uuid.UUID | None,
    role_filter: UserRole | None,
    is_active_filter: bool | None,
    search: str | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[UserSummary]:
    filters = _build_user_list_filters(
        current_user, village_id_filter, role_filter, is_active_filter, search
    )

    count_result = await db.execute(select(func.count()).select_from(User).where(*filters))
    total = count_result.scalar_one()

    stmt = (
        select(User, Group.name)
        .outerjoin(Group, User.village_id == Group.id)
        .where(*filters)
        .order_by(User.fullname)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    items = result.all()

    return PaginatedResponse[UserSummary](
        items=[
            UserSummary(
                id=user.id,
                username=user.username,
                role=user.role,
                village_id=user.village_id,
                village_name=village_name,
                is_active=user.is_active,
                is_verify=user.is_verify,
                created_at=user.created_at,
                avatar_url=_build_avatar_url(request, user),
            )
            for user, village_name in items
        ],                                 
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_own_user_detail(db: AsyncSession, request: Request, current_user: User) -> UserMeDetail:
    return await _to_user_me_detail(db, request, current_user)


async def get_user_detail(db: AsyncSession, request: Request, current_user: User, user_id: uuid.UUID) -> UserDetail:
    try:
        user = await _get_user_or_404(db, user_id)
        verify_village_scope(current_user, user.village_id)
    except HTTPException as e:
        if e.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UserErrors.NOT_FOUND) from e
        raise
    return await _to_user_detail(db, request, user)


async def set_user_active_status(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
    payload: UserStatusUpdate,
) -> UserDetail:
    try:
        target = await _get_user_or_404(db, user_id)
        _verify_user_write_scope(current_user, target)
    except HTTPException as e:
        if e.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UserErrors.NOT_FOUND) from e
        raise

    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ไม่สามารถเปลี่ยนสถานะบัญชีตัวเองได้",
        )

    previous_is_active = target.is_active
    target.is_active = payload.is_active

    if payload.is_active == previous_is_active:
        action = "user_updated"
        detail = f"updated user: {target.username}"
    elif payload.is_active:
        action = "user_activated"
        detail = f"user activated: {target.username}"
    else:
        action = "user_deactivated"
        detail = f"user deactivated: {target.username}"

    await audit_service.log_action(
        db,
        request,
        action=action,
        detail=detail,
        user_id=current_user.id,
        village_id=target.village_id,
    )

    if not payload.is_active and previous_is_active:
        await auth_service.revoke_all_refresh_tokens(db, target.id)

    await db.commit()
    await db.refresh(target)
    return await _to_user_detail(db, request, target)


async def reset_user_password(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
    payload: AdminResetPasswordRequest,
) -> str:
    target = await _get_user_or_404(db, user_id)
    _verify_password_reset_scope(current_user, target)

    if target.hashpassword is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UserErrors.PASSWORD_NOT_SET_YET,
        )

    target.hashpassword = await hash_password(payload.new_password)
    target.password_changed_at = datetime.now(timezone.utc)
    await auth_service.revoke_all_refresh_tokens(db, target.id)

    await audit_service.log_action(
        db,
        request,
        action="user_password_reset",
        detail=f"password reset for user: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()
    await db.refresh(target)
    return target.username


async def resend_invite(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    user_id: uuid.UUID,
) -> UserDetail:
    if email_service.is_email_service_degraded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ไม่สามารถส่งอีเมลเชิญได้ในขณะนี้เนื่องจากระบบอีเมลขัดข้อง",
        )

    target = await _get_user_or_404(db, user_id)
    _verify_user_write_scope(current_user, target)

    if target.is_verify:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UserErrors.ALREADY_VERIFIED,
        )

    last_sent_result = await db.execute(
        select(Verify.created_at)
        .where(Verify.user_id == target.id, Verify.type == VerifyType.INITIAL_SETUP)
        .order_by(Verify.created_at.desc())
        .limit(1)
    )
    last_sent_at = last_sent_result.scalar_one_or_none()
    if last_sent_at is not None and datetime.now(timezone.utc) - last_sent_at < _RESEND_INVITE_COOLDOWN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=UserErrors.RESEND_INVITE_COOLDOWN,
        )

    raw_token = await auth_service.create_verify_token(db, target, VerifyType.INITIAL_SETUP)

    await auth_service.invalidate_pending_verify_tokens(
        db, target.id, VerifyType.INITIAL_SETUP, exclude_token_hash=hash_token(raw_token)
    )

    await audit_service.log_action(
        db,
        request,
        action="user_invite_resent",
        detail=f"resent invite email: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()
    await db.refresh(target)

    background_tasks.add_task(email_service.send_invite_email_background, target.email, raw_token)

    return await _to_user_detail(db, request, target)


async def unlock_user_account(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
) -> str:
    target = await _get_user_or_404(db, user_id)
    _verify_user_write_scope(current_user, target)

    get_account_locker().reset(target.username.strip().lower())

    await audit_service.log_action(
        db,
        request,
        action="account_unlocked",
        detail=f"unlocked account: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()
    return target.username

async def delete_user(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
) -> None:
    target = await _get_user_or_404(db, user_id)

    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ไม่สามารถลบบัญชีตัวเองได้",
        )

    _verify_user_write_scope(current_user, target)

    avatar_path_to_delete = target.avatar_path

    await audit_service.log_action(
        db,
        request,
        action="user_deleted",
        detail=f"permanently deleted user: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    from app.models.notification import Notification
    await db.execute(delete(Notification).where(Notification.user_id == target.id))
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == target.id))
    
    # ลบ Refresh Token ออกจากฐานข้อมูล
    await auth_service.revoke_all_refresh_tokens(db, target.id)
    
    await db.delete(target)
    await db.commit()

    if avatar_path_to_delete:
        await storage_service.delete_image(avatar_path_to_delete)

async def list_locked_accounts(
    db: AsyncSession,
    current_user: User,
) -> list[LockedAccountEntry]:
    locked = get_account_locker().list_locked()
    if not locked:
        return []

    retry_after_by_username = dict(locked)

    stmt = (
        select(User, Group.name)
        .outerjoin(Group, User.village_id == Group.id)
        .where(User.username.in_(retry_after_by_username.keys()))
    )
    if current_user.role == UserRole.ADMIN:
        stmt = stmt.where(
            User.village_id == current_user.village_id,
            User.role == UserRole.USER,
        )

    result = await db.execute(stmt)
    rows = result.all()

    now = datetime.now(timezone.utc)
    entries = [
        LockedAccountEntry(
            user_id=user.id,
            username=user.username,
            fullname=user.fullname,
            role=user.role,
            village_id=user.village_id,
            village_name=village_name,
            unlocked_at=now + timedelta(seconds=retry_after_by_username[user.username]),
        )
        for user, village_name in rows
    ]
    entries.sort(key=lambda entry: entry.unlocked_at, reverse=True)
    return entries


async def update_user_fullname(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
    payload: UserFullnameUpdate,
) -> UserProfileRead:
    target = await _get_user_or_404(db, user_id)
    _verify_user_write_scope(current_user, target)

    target.fullname = payload.fullname

    await audit_service.log_action(
        db,
        request,
        action="user_fullname_updated",
        detail=f"updated fullname for user: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()
    await db.refresh(target)
    return UserProfileRead(
        id=target.id,
        username=target.username,
        fullname=target.fullname,
        email=target.email,
        role=target.role,
        village_id=target.village_id,
        avatar_url=_build_avatar_url(request, target),
    )


async def request_email_change(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    user_id: uuid.UUID,
    payload: EmailChangeRequest,
) -> None:
    if email_service.is_email_service_degraded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ไม่สามารถส่งอีเมลยืนยันการเปลี่ยนอีเมลได้ในขณะนี้เนื่องจากระบบอีเมลขัดข้อง",
        )

    target = await _get_user_or_404(db, user_id)
    _verify_user_write_scope(current_user, target)

    from app.core.rate_limit import get_rate_limiter, RateLimitExceeded
    rate_limiter = get_rate_limiter()
    
    try:
        rate_limiter.check(f"change_email_cooldown:{user_id}", 1, 300)
    except RateLimitExceeded as e:
        minutes_left = int(e.retry_after_seconds // 60) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"กรุณารออีก {minutes_left} นาที ก่อนขอเปลี่ยนอีเมลได้อีกครั้ง"
        ) from e
        
    try:
        rate_limiter.check(f"change_email_daily:{user_id}", 3, 86400)
    except RateLimitExceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="คุณขอเปลี่ยนอีเมลเกิน 3 ครั้งแล้ว กรุณาลองใหม่ในวันพรุ่งนี้"
        ) from None

    if payload.new_email == target.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=UserErrors.EMAIL_SAME_AS_CURRENT)

    existing_email_result = await db.execute(select(User.id).where(User.email == payload.new_email))
    if existing_email_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=UserErrors.EMAIL_ALREADY_IN_USE)

    last_sent_result = await db.execute(
        select(Verify.created_at)
        .where(Verify.user_id == target.id, Verify.type == VerifyType.EMAIL_CHANGE)
        .order_by(Verify.created_at.desc())
        .limit(1)
    )
    last_sent_at = last_sent_result.scalar_one_or_none()
    if last_sent_at is not None and datetime.now(timezone.utc) - last_sent_at < _EMAIL_CHANGE_COOLDOWN:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=UserErrors.EMAIL_CHANGE_COOLDOWN)

    raw_token = await auth_service.create_verify_token(
        db, target, VerifyType.EMAIL_CHANGE, new_email=payload.new_email
    )
    await auth_service.invalidate_pending_verify_tokens(
        db, target.id, VerifyType.EMAIL_CHANGE, exclude_token_hash=hash_token(raw_token)
    )

    await audit_service.log_action(
        db,
        request,
        action="email_change_requested",
        detail=f"requested email change to {payload.new_email} for user: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()

    background_tasks.add_task(
        email_service.send_email_change_confirmation_background, payload.new_email, raw_token
    )


async def upload_user_avatar(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
    file: UploadFile,
) -> UserProfileRead:
    target = await _get_user_or_404(db, user_id)
    _verify_user_write_scope(current_user, target)

    get_rate_limiter().check(
        f"avatar_upload:{target.id}", _AVATAR_UPLOAD_LIMIT, _AVATAR_UPLOAD_WINDOW_SECONDS
    )

    content, extension = await storage_service.read_and_validate_image(
        file, max_size_bytes=settings.user_avatar_max_size_bytes
    )

    image_id = uuid.uuid4()
    new_path = storage_service.build_avatar_path(target.id, image_id, extension)

    try:
        await storage_service.write_image(new_path, content)
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=AvatarErrors.STORE_FAILED,
        ) from None

    old_path = target.avatar_path
    is_replace = old_path is not None
    target.avatar_path = new_path

    await audit_service.log_action(
        db,
        request,
        action="user_avatar_replaced" if is_replace else "user_avatar_added",
        detail=(
            f"replaced avatar for user: {target.username}"
            if is_replace
            else f"added avatar for user: {target.username}"
        ),
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()
    await db.refresh(target)

    if old_path is not None:
        await storage_service.delete_image(old_path)

    return UserProfileRead(
        id=target.id,
        username=target.username,
        fullname=target.fullname,
        email=target.email,
        role=target.role,
        village_id=target.village_id,
        avatar_url=_build_avatar_url(request, target),
    )


async def delete_user_avatar(
    db: AsyncSession,
    request: Request,
    current_user: User,
    user_id: uuid.UUID,
) -> None:
    target = await _get_user_or_404(db, user_id)
    _verify_user_write_scope(current_user, target)

    if target.avatar_path is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AvatarErrors.ALREADY_NULL)

    old_path = target.avatar_path
    target.avatar_path = None

    await audit_service.log_action(
        db,
        request,
        action="user_avatar_removed",
        detail=f"removed avatar for user: {target.username}",
        user_id=current_user.id,
        village_id=target.village_id,
    )

    await db.commit()
    await storage_service.delete_image(old_path)


async def get_user_avatar_path(
    db: AsyncSession,
    current_user: User,
    user_id: uuid.UUID,
) -> tuple[Path, str]:
    target = await _get_user_or_404(db, user_id)
    _verify_avatar_view_scope(current_user, target)

    if target.avatar_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AvatarErrors.NOT_SET)

    absolute_path = storage_service.resolve_storage_path(target.avatar_path)
    if not absolute_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AvatarErrors.NOT_SET)

    return absolute_path, storage_service.guess_media_type(absolute_path)