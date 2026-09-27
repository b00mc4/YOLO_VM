from __future__ import annotations
import asyncio
import enum
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from fastapi import HTTPException, status
from sqlalchemy import select
from app.core.config import get_settings
from app.core.connection_limit import InMemoryConnectionLimiter
from app.core.security import generate_secure_token, hash_token
from app.core.sse_channel import try_emit
from app.db.session import async_session_maker
from app.models.group import Group
from app.models.user import User, UserRole
from app.schemas.presence import (
    AllVillagesPresenceSnapshot,
    PresenceUserEntry,
    VillageBreakdownEntry,
    VillagePresenceSnapshot,
)
from app.core.error_messages import Common, RealtimeErrors

settings = get_settings()


_MAX_TRACKED_TICKETS = 10_000
_BROADCAST_QUEUE_MAXSIZE = 20


class PresenceViewScope(str, enum.Enum):
    NONE = "none"
    OWN_VILLAGE = "own_village"
    SINGLE_VILLAGE = "single_village"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class _PresenceTicketData:
    user_id: uuid.UUID
    username: str
    fullname: str
    role: UserRole
    village_id: uuid.UUID | None
    view_scope: PresenceViewScope
    view_village_id: uuid.UUID | None
    expire_at: datetime
    password_changed_at: datetime


@dataclass(frozen=True, slots=True)
class _PresenceConn:
    user_id: uuid.UUID
    username: str
    fullname: str
    role: UserRole
    village_id: uuid.UUID | None


_presence_tickets: OrderedDict[str, _PresenceTicketData] = OrderedDict()
_last_ticket_sweep_at = monotonic()

_presence_connections: dict[uuid.UUID, _PresenceConn] = {}
_presence_by_village: dict[uuid.UUID, dict[uuid.UUID, set[uuid.UUID]]] = defaultdict(
    lambda: defaultdict(set)
)
_presence_superadmins: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
_connection_limiter = InMemoryConnectionLimiter()

_broadcast_subscribers_by_village: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)
_broadcast_subscribers_all: set[asyncio.Queue] = set()


def _sweep_expired_tickets() -> None:
    global _last_ticket_sweep_at

    now_monotonic = monotonic()
    if now_monotonic - _last_ticket_sweep_at < settings.presence_sweep_interval_seconds:
        return
    _last_ticket_sweep_at = now_monotonic

    now_utc = datetime.now(timezone.utc)
    expired_keys = [
        token_hash
        for token_hash, data in _presence_tickets.items()
        if data.expire_at < now_utc
    ]
    for token_hash in expired_keys:
        _presence_tickets.pop(token_hash, None)


def issue_presence_ticket(
    current_user: User,
    requested_village_id: uuid.UUID | None,
) -> str:
    if requested_village_id is not None and current_user.role != UserRole.SUPERADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
        )

    if current_user.role in (UserRole.USER, UserRole.ADMIN):
        view_scope = PresenceViewScope.OWN_VILLAGE
        view_village_id = current_user.village_id
    else:
        if requested_village_id is not None:
            view_scope = PresenceViewScope.SINGLE_VILLAGE
            view_village_id = requested_village_id
        else:
            view_scope = PresenceViewScope.ALL
            view_village_id = None

    _sweep_expired_tickets()

    if len(_presence_tickets) >= _MAX_TRACKED_TICKETS:
        _presence_tickets.popitem(last=False)

    raw_token = generate_secure_token()
    expire_at = datetime.now(timezone.utc) + timedelta(seconds=settings.sse_ticket_expire_seconds)

    _presence_tickets[hash_token(raw_token)] = _PresenceTicketData(
        user_id=current_user.id,
        username=current_user.username,
        fullname=current_user.fullname,
        role=current_user.role,
        village_id=current_user.village_id,
        view_scope=view_scope,
        view_village_id=view_village_id,
        expire_at=expire_at,
        password_changed_at=current_user.password_changed_at,
    )
    return raw_token


def resolve_presence_ticket(raw_token: str) -> _PresenceTicketData:
    data = _presence_tickets.pop(hash_token(raw_token), None)
    if data is None or data.expire_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=RealtimeErrors.INVALID_OR_EXPIRED_TICKET,
        )
    return data


def _representative_conn(conn_ids: set[uuid.UUID]) -> _PresenceConn | None:
    for conn_id in conn_ids:
        conn = _presence_connections.get(conn_id)
        if conn is not None:
            return conn
    return None


def _online_superadmins() -> list[PresenceUserEntry]:
    entries: list[PresenceUserEntry] = []
    for conn_ids in _presence_superadmins.values():
        conn = _representative_conn(conn_ids)
        if conn is None:
            continue
        entries.append(
            PresenceUserEntry(
                user_id=conn.user_id,
                username=conn.username,
                fullname=conn.fullname,
                role=conn.role,
            )
        )
    return entries


def _online_users_in_village(village_id: uuid.UUID) -> list[PresenceUserEntry]:
    users_in_village = _presence_by_village.get(village_id, {})
    entries: list[PresenceUserEntry] = []
    for conn_ids in users_in_village.values():
        conn = _representative_conn(conn_ids)
        if conn is None:
            continue
        entries.append(
            PresenceUserEntry(
                user_id=conn.user_id,
                username=conn.username,
                fullname=conn.fullname,
                role=conn.role,
            )
        )
    return entries


async def _build_village_snapshot(village_id: uuid.UUID) -> VillagePresenceSnapshot:
    online_users = _online_users_in_village(village_id)
    online_superadmins = _online_superadmins()

    return VillagePresenceSnapshot(
        village_id=village_id,
        total_online=len(online_users) + len(online_superadmins),
        online_users=online_users,
        online_superadmins=online_superadmins,
    )


async def _fetch_village_names(village_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not village_ids:
        return {}
    async with async_session_maker() as db:
        result = await db.execute(select(Group.id, Group.name).where(Group.id.in_(village_ids)))
        return {row.id: row.name for row in result.all()}


async def _build_all_villages_snapshot() -> AllVillagesPresenceSnapshot:
    village_ids = [vid for vid, users in _presence_by_village.items() if users]
    village_names = await _fetch_village_names(village_ids)
    online_superadmins = _online_superadmins()

    villages: list[VillageBreakdownEntry] = []
    total_villager_online = 0
    for village_id in village_ids:
        online_users = _online_users_in_village(village_id)
        if not online_users:
            continue
        total_villager_online += len(online_users)
        villages.append(
            VillageBreakdownEntry(
                village_id=village_id,
                village_name=village_names.get(village_id, "Unknown"),
                total_online=len(online_users),
                online_users=online_users,
            )
        )

    villages.sort(key=lambda v: v.village_name)
    return AllVillagesPresenceSnapshot(
        total_online=total_villager_online + len(online_superadmins),
        villages=villages,
        online_superadmins=online_superadmins,
    )


async def build_snapshot_for_ticket(ticket_data: _PresenceTicketData) -> dict | None:
    if ticket_data.view_scope == PresenceViewScope.NONE:
        return None
    if ticket_data.view_scope in (PresenceViewScope.OWN_VILLAGE, PresenceViewScope.SINGLE_VILLAGE):
        snapshot = await _build_village_snapshot(ticket_data.view_village_id)
        return snapshot.model_dump(mode="json")
    snapshot = await _build_all_villages_snapshot()
    return snapshot.model_dump(mode="json")


def _subscribers_watching_village(village_id: uuid.UUID) -> list[asyncio.Queue]:
    return list(_broadcast_subscribers_by_village.get(village_id, set())) + list(
        _broadcast_subscribers_all
    )


async def _broadcast_to_village_watchers(village_id: uuid.UUID) -> None:
    watchers = _subscribers_watching_village(village_id)
    if not watchers:
        return

    single_village_snapshot = (await _build_village_snapshot(village_id)).model_dump(mode="json")
    all_snapshot_cache: dict | None = None

    for queue in watchers:
        if queue in _broadcast_subscribers_all:
            if all_snapshot_cache is None:
                all_snapshot_cache = (await _build_all_villages_snapshot()).model_dump(mode="json")
            item = {"event": "presence_update", "data": all_snapshot_cache}
            if not try_emit(queue, item):
                _broadcast_subscribers_all.discard(queue)
        else:
            item = {"event": "presence_update", "data": single_village_snapshot}
            if not try_emit(queue, item):
                subscribers = _broadcast_subscribers_by_village.get(village_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        _broadcast_subscribers_by_village.pop(village_id, None)


async def _broadcast_superadmin_change() -> None:
    has_any_watcher = any(_broadcast_subscribers_by_village.values()) or _broadcast_subscribers_all
    if not has_any_watcher:
        return

    village_snapshot_cache: dict[uuid.UUID, dict] = {}

    for village_id, subscribers in list(_broadcast_subscribers_by_village.items()):
        if not subscribers:
            continue
        if village_id not in village_snapshot_cache:
            village_snapshot_cache[village_id] = (
                await _build_village_snapshot(village_id)
            ).model_dump(mode="json")

        item = {"event": "presence_update", "data": village_snapshot_cache[village_id]}
        dead = [queue for queue in list(subscribers) if not try_emit(queue, item)]
        for queue in dead:
            subscribers.discard(queue)
        if not subscribers:
            _broadcast_subscribers_by_village.pop(village_id, None)

    if _broadcast_subscribers_all:
        all_snapshot_cache = (await _build_all_villages_snapshot()).model_dump(mode="json")
        item = {"event": "presence_update", "data": all_snapshot_cache}
        dead = [queue for queue in list(_broadcast_subscribers_all) if not try_emit(queue, item)]
        for queue in dead:
            _broadcast_subscribers_all.discard(queue)


def register_watcher(ticket_data: _PresenceTicketData) -> asyncio.Queue | None:
    if ticket_data.view_scope == PresenceViewScope.NONE:
        return None

    queue: asyncio.Queue = asyncio.Queue(maxsize=_BROADCAST_QUEUE_MAXSIZE)
    if ticket_data.view_scope == PresenceViewScope.ALL:
        _broadcast_subscribers_all.add(queue)
    else:
        _broadcast_subscribers_by_village[ticket_data.view_village_id].add(queue)
    return queue


def unregister_watcher(ticket_data: _PresenceTicketData, queue: asyncio.Queue | None) -> None:
    if queue is None:
        return
    if ticket_data.view_scope == PresenceViewScope.ALL:
        _broadcast_subscribers_all.discard(queue)
    else:
        subscribers = _broadcast_subscribers_by_village.get(ticket_data.view_village_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                _broadcast_subscribers_by_village.pop(ticket_data.view_village_id, None)


async def register_connection(ticket_data: _PresenceTicketData) -> uuid.UUID:
    _connection_limiter.register(ticket_data.user_id, settings.presence_max_connections_per_user)

    conn_id = uuid.uuid4()

    _presence_connections[conn_id] = _PresenceConn(
        user_id=ticket_data.user_id,
        username=ticket_data.username,
        fullname=ticket_data.fullname,
        role=ticket_data.role,
        village_id=ticket_data.village_id,
    )

    if ticket_data.village_id is None:
        was_first_connection = not _presence_superadmins.get(ticket_data.user_id)
        _presence_superadmins[ticket_data.user_id].add(conn_id)
        if was_first_connection:
            await _broadcast_superadmin_change()
    else:
        village_users = _presence_by_village[ticket_data.village_id]
        was_first_connection = not village_users.get(ticket_data.user_id)
        village_users[ticket_data.user_id].add(conn_id)
        if was_first_connection:
            await _broadcast_to_village_watchers(ticket_data.village_id)

    return conn_id


async def unregister_connection(conn_id: uuid.UUID) -> None:
    conn = _presence_connections.pop(conn_id, None)
    if conn is None:
        return

    _connection_limiter.unregister(conn.user_id)

    if conn.village_id is None:
        user_conns = _presence_superadmins.get(conn.user_id)
        if user_conns is None:
            return
        user_conns.discard(conn_id)
        became_offline = not user_conns
        if became_offline:
            _presence_superadmins.pop(conn.user_id, None)
            await _broadcast_superadmin_change()
        return

    village_users = _presence_by_village.get(conn.village_id)
    if village_users is None:
        return

    user_conns = village_users.get(conn.user_id)
    if user_conns is None:
        return

    user_conns.discard(conn_id)
    became_offline = not user_conns
    if became_offline:
        village_users.pop(conn.user_id, None)
    if not village_users:
        _presence_by_village.pop(conn.village_id, None)
    if became_offline:
        await _broadcast_to_village_watchers(conn.village_id)