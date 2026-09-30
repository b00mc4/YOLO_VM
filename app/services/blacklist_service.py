from __future__ import annotations
import logging
import uuid
from datetime import datetime
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import verify_village_scope
from app.core.alert_cooldown import get_alert_cooldown
from app.core.timezone import BANGKOK_TZ
from app.db.session import async_session_maker
from app.models.blacklist import Blacklist
from app.models.whitelist import Whitelist
from app.models.user import User, UserRole
from app.schemas.blacklist import BlacklistCreate, BlacklistRead, BlacklistUpdate
from app.schemas.common import PaginatedResponse
from app.services import audit_service, email_service
from app.core.error_messages import BlacklistErrors


from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


from app.core.scope_utils import resolve_village_id, build_scope_filters
from app.core.db_utils import escape_like

async def _get_entry_or_404(db: AsyncSession, entry_id: uuid.UUID) -> Blacklist:
    result = await db.execute(select(Blacklist).where(Blacklist.id == entry_id))
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=BlacklistErrors.NOT_FOUND)
    return entry

async def _get_entry_or_404(db: AsyncSession, entry_id: uuid.UUID) -> Blacklist:
    result = await db.execute(select(Blacklist).where(Blacklist.id == entry_id))
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=BlacklistErrors.NOT_FOUND)
    return entry


async def _check_not_whitelisted(
    db: AsyncSession,
    village_id: uuid.UUID,
    license_plate: str,
    province: str,
) -> None:
    result = await db.execute(
        select(Whitelist.id)
        .where(
            Whitelist.village_id == village_id,
            Whitelist.license_plate == license_plate,
            Whitelist.province == province,
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=BlacklistErrors.PLATE_IS_WHITELISTED,
        )


async def create_blacklist_entry(
    db: AsyncSession,
    request: Request,
    current_user: User,
    payload: BlacklistCreate,
) -> BlacklistRead:
    village_id = await resolve_village_id(db, current_user, payload.village_id)
    await _check_not_whitelisted(db, village_id, payload.license_plate, payload.province)

    existing = await db.execute(
        select(Blacklist.id).where(
            Blacklist.village_id == village_id,
            Blacklist.license_plate == payload.license_plate,
            Blacklist.province == payload.province,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ป้ายทะเบียนนี้มีอยู่ในบัญชีดำของหมู่บ้านนี้แล้ว",
        )

    entry = Blacklist(
        village_id=village_id,
        license_plate=payload.license_plate,
        province=payload.province,
        reason=payload.reason,
        added_by=current_user.id,
    )
    db.add(entry)

    await audit_service.log_action(
        db,
        request,
        action="blacklist_create",
        detail=f"added blacklist entry: {payload.license_plate} ({payload.province})",
        user_id=current_user.id,
        village_id=village_id,
    )

    await db.commit()
    await db.refresh(entry)
    return BlacklistRead.model_validate(entry)


async def list_blacklist_entries(
    db: AsyncSession,
    current_user: User,
    village_id: uuid.UUID | None,
    license_plate: str | None,
    province: str | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[BlacklistRead]:
    scope_filters = build_scope_filters(current_user, village_id, Blacklist)
    stmt = select(Blacklist).where(*scope_filters)

    if license_plate is not None:
        stmt = stmt.where(Blacklist.license_plate.ilike(f"%{escape_like(license_plate)}%", escape="\\"))
    if province is not None:
        stmt = stmt.where(Blacklist.province == province)

    count_stmt = stmt.with_only_columns(func.count()).order_by(None)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    stmt = (
        stmt.order_by(Blacklist.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    items = result.scalars().all()

    return PaginatedResponse[BlacklistRead](
        items=[BlacklistRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


async def update_blacklist_entry(
    db: AsyncSession,
    request: Request,
    current_user: User,
    entry_id: uuid.UUID,
    payload: BlacklistUpdate,
) -> BlacklistRead:
    entry = await _get_entry_or_404(db, entry_id)
    verify_village_scope(current_user, entry.village_id)

    update_data = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in update_data.items():
        setattr(entry, field, value)

    await _check_not_whitelisted(db, entry.village_id, entry.license_plate, entry.province)

    await audit_service.log_action(
        db,
        request,
        action="blacklist_update",
        detail=f"updated blacklist entry reason: {entry.license_plate} ({entry.province})",
        user_id=current_user.id,
        village_id=entry.village_id,
    )

    await db.commit()
    await db.refresh(entry)
    return BlacklistRead.model_validate(entry)


async def delete_blacklist_entry(
    db: AsyncSession,
    request: Request,
    current_user: User,
    entry_id: uuid.UUID,
) -> None:
    entry = await _get_entry_or_404(db, entry_id)
    verify_village_scope(current_user, entry.village_id)

    await audit_service.log_action(
        db,
        request,
        action="blacklist_delete",
        detail=f"removed blacklist entry: {entry.license_plate} ({entry.province})",
        user_id=current_user.id,
        village_id=entry.village_id,
    )

    await db.delete(entry)
    await db.commit()


def _cooldown_key(village_id: uuid.UUID, license_plate: str, province: str) -> str:
    return f"blacklist_email:{village_id}:{license_plate}:{province}"


async def _log_skip(
    village_id: uuid.UUID,
    camera_name: str,
    license_plate: str,
    province: str,
    reason: str,
) -> None:
    async with async_session_maker() as db:
        await audit_service.log_action(
            db,
            request=None,
            action="blacklist_email_alert_skipped",
            detail=(
                f"blacklist email alert skipped for '{camera_name}' "
                f"({license_plate}/{province}): {reason}"
            ),
            village_id=village_id,
        )
        await db.commit()


async def handle_blacklist_detection(
    camera_id: uuid.UUID,
    village_id: uuid.UUID,
    camera_name: str,
    license_plate: str,
    province: str,
    time_detect: datetime,
) -> None:
    if email_service.is_email_service_degraded():
        await _log_skip(
            village_id, camera_name, license_plate, province,
            "email service is currently degraded",
        )
        return

    if not get_alert_cooldown().allow(
        _cooldown_key(village_id, license_plate, province), settings.blacklist_email_alert_cooldown_seconds
    ):
        return

    async with async_session_maker() as db:
        result = await db.execute(
            select(User.email).where(
                User.village_id == village_id,
                User.role.in_((UserRole.USER, UserRole.ADMIN)),
                User.is_active.is_(True),
                User.email.is_not(None),
            )
        )
        recipients = list(result.scalars().all())

    if not recipients:
        return

    detected_at_local = time_detect.astimezone(BANGKOK_TZ).strftime("%d/%m/%Y %H:%M:%S")

    try:
        failed_recipients = await email_service.send_blacklist_alert_email(
            recipients,
            camera_name,
            license_plate,
            province,
            detected_at_local,
        )
    except Exception:
        logger.exception(
            "Failed to send blacklist alert email for camera_id=%s plate=%s/%s",
            camera_id, license_plate, province,
        )
        await _log_skip(village_id, camera_name, license_plate, province, "smtp send failed")
        return

    if failed_recipients:
        logger.warning(
            "Blacklist alert email partially failed for camera_id=%s plate=%s/%s: %s",
            camera_id, license_plate, province, failed_recipients,
        )