from __future__ import annotations
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.notification import Notification
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse
from app.schemas.notification import NotificationRead
from app.core.error_messages import NotificationErrors

logger = logging.getLogger(__name__)

_DETAIL_MAX_LENGTH = 1000
_RETENTION_DAYS = 30


async def _fan_out(
    db: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    village_id: uuid.UUID | None,
    action: str,
    detail: str,
    payload: dict[str, Any] | None,
) -> None:
    truncated_detail = detail[:_DETAIL_MAX_LENGTH]
    notifications = [
        Notification(
            user_id=user_id,
            village_id=village_id,
            action=action,
            detail=truncated_detail,
            payload=payload,
        )
        for user_id in user_ids
    ]
    if not notifications:
        return
    db.add_all(notifications)
    await db.flush()


async def notify_village(
    db: AsyncSession,
    village_id: uuid.UUID,
    action: str,
    detail: str,
    payload: dict[str, Any] | None = None,
    roles: tuple[UserRole, ...] = (UserRole.USER, UserRole.ADMIN),
) -> None:
    result = await db.execute(
        select(User.id).where(
            User.village_id == village_id,
            User.role.in_(roles),
            User.is_active.is_(True),
        )
    )
    await _fan_out(db, result.scalars().all(), village_id, action, detail, payload)


async def notify_superadmins(
    db: AsyncSession,
    action: str,
    detail: str,
    payload: dict[str, Any] | None = None,
) -> None:
    result = await db.execute(
        select(User.id).where(User.role == UserRole.SUPERADMIN, User.is_active.is_(True))
    )
    await _fan_out(db, result.scalars().all(), None, action, detail, payload)


async def list_notifications(
    db: AsyncSession,
    current_user: User,
    is_read: bool | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[NotificationRead]:
    filters = [Notification.user_id == current_user.id]
    if is_read is not None:
        filters.append(Notification.is_read == is_read)

    count_result = await db.execute(select(func.count()).select_from(Notification).where(*filters))
    total = count_result.scalar_one()

    stmt = (
        select(Notification)
        .where(*filters)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    items = result.scalars().all()

    return PaginatedResponse[NotificationRead](
        items=[NotificationRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_unread_count(db: AsyncSession, current_user: User) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read.is_(False))
    )
    return result.scalar_one()


async def mark_notification_read(
    db: AsyncSession,
    current_user: User,
    notification_id: uuid.UUID,
) -> NotificationRead:
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NotificationErrors.NOT_FOUND)

    notification.is_read = True
    await db.commit()
    await db.refresh(notification)
    return NotificationRead.model_validate(notification)


async def mark_all_notifications_read(db: AsyncSession, current_user: User) -> int:
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await db.commit()
    return result.rowcount or 0


async def cleanup_old_notifications(db: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)
    result = await db.execute(delete(Notification).where(Notification.created_at < cutoff))
    await db.commit()
    return result.rowcount or 0