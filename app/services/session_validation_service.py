from __future__ import annotations
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.session_manager import session_manager
from app.core.stream_registry import StreamCloseCode, StreamHandle, stream_registry
from app.models.group import Group
from app.models.user import User


@dataclass(frozen=True, slots=True)
class _UserState:
    is_active: bool
    password_changed_at: datetime | None
    village_id: uuid.UUID | None
    village_is_active: bool | None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _fetch_user_states(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, _UserState]:
    result = await db.execute(
        select(
            User.id,
            User.is_active,
            User.password_changed_at,
            User.village_id,
            Group.is_active.label("village_is_active"),
        )
        .outerjoin(Group, Group.id == User.village_id)
        .where(User.id.in_(list(user_ids)))
    )
    return {
        row.id: _UserState(
            is_active=row.is_active,
            password_changed_at=row.password_changed_at,
            village_id=row.village_id,
            village_is_active=row.village_is_active,
        )
        for row in result.all()
    }


def _is_stream_valid(handle: StreamHandle, state: _UserState | None) -> bool:
    identity = handle.identity
    if state is None or not state.is_active:
        return False

    if not session_manager.is_valid_session(identity.user_id, identity.session_id):
        return False

    if state.password_changed_at is not None and identity.password_changed_at is not None:
        if _as_utc(state.password_changed_at) > _as_utc(identity.password_changed_at):
            return False

    if identity.village_id is None:
        return True

    if state.village_id != identity.village_id:
        return False

    return bool(state.village_is_active)


async def revoke_invalid_streams(db: AsyncSession) -> int:
    handles = stream_registry.snapshot()
    if not handles:
        return 0

    states = await _fetch_user_states(db, {handle.identity.user_id for handle in handles})
    invalid_ids = [
        handle.stream_id
        for handle in handles
        if not _is_stream_valid(handle, states.get(handle.identity.user_id))
    ]
    return stream_registry.revoke(invalid_ids, StreamCloseCode.SESSION_REVOKED)
