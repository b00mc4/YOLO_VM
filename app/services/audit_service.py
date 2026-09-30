from __future__ import annotations
import uuid
from datetime import datetime
from typing import Literal
from fastapi import Request, HTTPException, status
from sqlalchemy import func, select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.audit_log import AuditLog
from app.models.group import Group
from app.models.user import User, UserRole
from app.schemas.audit_log import AuditLogRead, AuditLogAction
from app.schemas.common import PaginatedResponse
from app.core.request_utils import get_client_ip
from app.core.error_messages import Common


_DETAIL_MAX_LENGTH = 1000
_USER_AGENT_MAX_LENGTH = 512

_BACKGROUND_IP_ADDRESS = "background"
_BACKGROUND_USER_AGENT = "background-task"

_SYSTEM_WIDE_ACTIONS = (
    AuditLogAction.streaming_server_down.value,
    AuditLogAction.streaming_server_recovered.value,
)


async def log_action(
    db: AsyncSession,
    request: Request | None,
    action: str | AuditLogAction,
    detail: str,
    user_id: uuid.UUID | None = None,
    village_id: uuid.UUID | None = None,
    actor_username: str | None = None,
):
    if request is not None:
        ip_address = get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")[:_USER_AGENT_MAX_LENGTH]
    else:
        ip_address = _BACKGROUND_IP_ADDRESS
        user_agent = _BACKGROUND_USER_AGENT

    if actor_username is None and user_id is not None:
        if request is not None and hasattr(request.state, "user") and request.state.user.id == user_id:
            actor_username = request.state.user.username
        else:
            user_result = await db.execute(select(User.username).where(User.id == user_id))
            actor_username = user_result.scalar_one_or_none()
            
    # Convert AuditLogAction enum to string for database
    action_str = action.value if isinstance(action, AuditLogAction) else action

    entry = AuditLog(
        village_id=village_id,
        user_id=user_id,
        actor_username=actor_username,
        action=action_str,
        detail=detail[:_DETAIL_MAX_LENGTH],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(entry)
    await db.flush()


def _build_audit_log_filters(
    current_user: User,
    village_id_filter: uuid.UUID | None,
    user_id_filter: uuid.UUID | None,
    action_filter: AuditLogAction | None,
    created_at_from: datetime | None,
    created_at_to: datetime | None,
) -> list:
    filters: list = []

    if current_user.role == UserRole.SUPERADMIN:
        if village_id_filter is not None:
            filters.append(AuditLog.village_id == village_id_filter)
    else:
        if village_id_filter is not None and village_id_filter != current_user.village_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
            )
        filters.append(
            or_(
                AuditLog.village_id == current_user.village_id,
                and_(AuditLog.village_id.is_(None), AuditLog.action.in_(_SYSTEM_WIDE_ACTIONS)),
            )
        )
        filters.append(
            or_(
                User.role != UserRole.SUPERADMIN,
                AuditLog.user_id.is_(None),
                User.id.is_(None)
            )
        )

    if user_id_filter is not None:
        filters.append(AuditLog.user_id == user_id_filter)
    if action_filter is not None:
        filters.append(AuditLog.action == action_filter)
    if created_at_from is not None:
        filters.append(AuditLog.created_at >= created_at_from)
    if created_at_to is not None:
        filters.append(AuditLog.created_at <= created_at_to)

    return filters


async def list_audit_logs(
    db: AsyncSession,
    current_user: User,
    village_id_filter: uuid.UUID | None,
    user_id_filter: uuid.UUID | None,
    action_filter: AuditLogAction | None,
    created_at_from: datetime | None,
    created_at_to: datetime | None,
    order: Literal["asc", "desc"],
    page: int,
    page_size: int,
) -> PaginatedResponse[AuditLogRead]:
    filters = _build_audit_log_filters(
        current_user,
        village_id_filter,
        user_id_filter,
        action_filter,
        created_at_from,
        created_at_to,
    )

    count_stmt = select(func.count()).select_from(AuditLog)
    stmt = select(AuditLog, Group.name).outerjoin(Group, AuditLog.village_id == Group.id)

    if current_user.role != UserRole.SUPERADMIN:
        count_stmt = count_stmt.outerjoin(User, AuditLog.user_id == User.id)
        stmt = stmt.outerjoin(User, AuditLog.user_id == User.id)

    count_stmt = count_stmt.where(*filters)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    order_col = AuditLog.created_at.asc() if order == "asc" else AuditLog.created_at.desc()
    stmt = (
        stmt.where(*filters)
        .order_by(order_col)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    rows = result.all()

    items = [
        AuditLogRead(
            id=entry.id,
            village_id=entry.village_id,
            village_name=village_name,
            user_id=entry.user_id,
            username=entry.actor_username,
            action=entry.action,
            detail=entry.detail,
            ip_address=entry.ip_address,
            user_agent=entry.user_agent,
            created_at=entry.created_at,
        )
        for entry, village_name in rows
    ]

    return PaginatedResponse[AuditLogRead](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )