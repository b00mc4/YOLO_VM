from __future__ import annotations
import asyncio
import enum
import logging
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from fastapi import HTTPException, status
from sqlalchemy import select
from app.core.config import get_settings
from app.core.security import generate_secure_token, hash_token
from app.core.sse_channel import StreamIdentity, try_emit
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
logger = logging.getLogger(__name__)


_MAX_TRACKED_TICKETS = 10_000
_PRESENCE_EVENT = "presence_update"


class PresenceViewScope(str, enum.Enum):
    NONE = "none"
    OWN_VILLAGE = "own_village"
    SINGLE_VILLAGE = "single_village"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class _PresenceTicketData:
    user_id: uuid.UUID
    session_id: uuid.UUID
    username: str
    fullname: str
    role: UserRole
    village_id: uuid.UUID | None
    view_scope: PresenceViewScope
    view_village_id: uuid.UUID | None
    expire_at: datetime
    password_changed_at: datetime

    def to_stream_identity(self) -> StreamIdentity:
        return StreamIdentity(
            user_id=self.user_id,
            session_id=self.session_id,
            village_id=self.village_id,
            password_changed_at=self.password_changed_at,
        )


PresenceTicketData = _PresenceTicketData


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
    session_id: uuid.UUID,
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
        session_id=session_id,
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


def _to_entry(conn: _PresenceConn) -> PresenceUserEntry:
    return PresenceUserEntry(
        user_id=conn.user_id,
        username=conn.username,
        fullname=conn.fullname,
        role=conn.role,
    )


def _online_superadmins() -> list[PresenceUserEntry]:
    entries: list[PresenceUserEntry] = []
    for conn_ids in _presence_superadmins.values():
        conn = _representative_conn(conn_ids)
        if conn is not None:
            entries.append(_to_entry(conn))
    return entries


def _online_users_in_village(village_id: uuid.UUID) -> list[PresenceUserEntry]:
    entries: list[PresenceUserEntry] = []
    for conn_ids in _presence_by_village.get(village_id, {}).values():
        conn = _representative_conn(conn_ids)
        if conn is not None:
            entries.append(_to_entry(conn))
    return entries


def _build_village_snapshot(village_id: uuid.UUID) -> dict:
    online_users = _online_users_in_village(village_id)
    online_superadmins = _online_superadmins()
    return VillagePresenceSnapshot(
        village_id=village_id,
        total_online=len(online_users) + len(online_superadmins),
        online_users=online_users,
        online_superadmins=online_superadmins,
    ).model_dump(mode="json")


async def _fetch_village_names(village_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not village_ids:
        return {}
    async with async_session_maker() as db:
        result = await db.execute(select(Group.id, Group.name).where(Group.id.in_(village_ids)))
        return {row.id: row.name for row in result.all()}


async def _build_all_villages_snapshot() -> dict:
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
    ).model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class _BroadcastBatch:
    dirty_villages: frozenset[uuid.UUID]
    superadmin_changed: bool
    pending_initial: tuple[tuple[asyncio.Queue, _PresenceTicketData], ...]

    @property
    def has_changes(self) -> bool:
        return self.superadmin_changed or bool(self.dirty_villages)

    @property
    def needs_all_snapshot(self) -> bool:
        if self.has_changes and _broadcast_subscribers_all:
            return True
        return any(ticket.view_scope == PresenceViewScope.ALL for _, ticket in self.pending_initial)


class _BroadcastScheduler:
    def __init__(self) -> None:
        self._dirty_villages: set[uuid.UUID] = set()
        self._superadmin_changed = False
        self._pending_initial: list[tuple[asyncio.Queue, _PresenceTicketData]] = []
        self._wake = asyncio.Event()

    def mark_village(self, village_id: uuid.UUID) -> None:
        self._dirty_villages.add(village_id)
        self._wake.set()

    def mark_superadmins(self) -> None:
        self._superadmin_changed = True
        self._wake.set()

    def request_initial(self, queue: asyncio.Queue, ticket_data: _PresenceTicketData) -> None:
        self._pending_initial.append((queue, ticket_data))
        self._wake.set()

    async def next_batch(self, debounce_seconds: float) -> _BroadcastBatch:
        await self._wake.wait()
        if debounce_seconds > 0:
            await asyncio.sleep(debounce_seconds)
        self._wake.clear()
        batch = _BroadcastBatch(
            dirty_villages=frozenset(self._dirty_villages),
            superadmin_changed=self._superadmin_changed,
            pending_initial=tuple(self._pending_initial),
        )
        self._dirty_villages.clear()
        self._superadmin_changed = False
        self._pending_initial.clear()
        return batch


_scheduler = _BroadcastScheduler()


def _is_watching(queue: asyncio.Queue, ticket_data: _PresenceTicketData) -> bool:
    if ticket_data.view_scope == PresenceViewScope.ALL:
        return queue in _broadcast_subscribers_all
    return queue in _broadcast_subscribers_by_village.get(ticket_data.view_village_id, ())


def _emit(subscribers: set[asyncio.Queue], data: dict, delivered: set[asyncio.Queue]) -> None:
    item = {"event": _PRESENCE_EVENT, "data": data}
    for queue in list(subscribers):
        if try_emit(queue, item):
            delivered.add(queue)
        else:
            subscribers.discard(queue)


async def _flush(batch: _BroadcastBatch) -> None:
    all_snapshot = await _build_all_villages_snapshot() if batch.needs_all_snapshot else None

    village_snapshots: dict[uuid.UUID, dict] = {}

    def village_snapshot(village_id: uuid.UUID) -> dict:
        if village_id not in village_snapshots:
            village_snapshots[village_id] = _build_village_snapshot(village_id)
        return village_snapshots[village_id]

    delivered: set[asyncio.Queue] = set()

    if batch.superadmin_changed:
        target_villages = list(_broadcast_subscribers_by_village)
    else:
        target_villages = [vid for vid in batch.dirty_villages if vid in _broadcast_subscribers_by_village]

    for village_id in target_villages:
        subscribers = _broadcast_subscribers_by_village.get(village_id)
        if not subscribers:
            continue
        _emit(subscribers, village_snapshot(village_id), delivered)
        if not subscribers:
            _broadcast_subscribers_by_village.pop(village_id, None)

    if all_snapshot is not None and batch.has_changes and _broadcast_subscribers_all:
        _emit(_broadcast_subscribers_all, all_snapshot, delivered)

    for queue, ticket_data in batch.pending_initial:
        if queue in delivered or not _is_watching(queue, ticket_data):
            continue
        if ticket_data.view_scope == PresenceViewScope.ALL:
            data = all_snapshot
        else:
            data = village_snapshot(ticket_data.view_village_id)
        if data is not None:
            try_emit(queue, {"event": _PRESENCE_EVENT, "data": data})


async def run_broadcaster() -> None:
    while True:
        batch = await _scheduler.next_batch(settings.presence_broadcast_debounce_seconds)
        try:
            await _flush(batch)
        except Exception:
            logger.exception("Presence broadcast flush failed")


def register_watcher(ticket_data: _PresenceTicketData, queue: asyncio.Queue) -> None:
    if ticket_data.view_scope == PresenceViewScope.NONE:
        return
    if ticket_data.view_scope == PresenceViewScope.ALL:
        _broadcast_subscribers_all.add(queue)
    else:
        _broadcast_subscribers_by_village[ticket_data.view_village_id].add(queue)
    _scheduler.request_initial(queue, ticket_data)


def unregister_watcher(ticket_data: _PresenceTicketData, queue: asyncio.Queue) -> None:
    if ticket_data.view_scope == PresenceViewScope.ALL:
        _broadcast_subscribers_all.discard(queue)
        return
    subscribers = _broadcast_subscribers_by_village.get(ticket_data.view_village_id)
    if subscribers is None:
        return
    subscribers.discard(queue)
    if not subscribers:
        _broadcast_subscribers_by_village.pop(ticket_data.view_village_id, None)


def register_connection(ticket_data: _PresenceTicketData) -> uuid.UUID:
    conn_id = uuid.uuid4()
    _presence_connections[conn_id] = _PresenceConn(
        user_id=ticket_data.user_id,
        username=ticket_data.username,
        fullname=ticket_data.fullname,
        role=ticket_data.role,
        village_id=ticket_data.village_id,
    )

    if ticket_data.village_id is None:
        user_conns = _presence_superadmins[ticket_data.user_id]
        if not user_conns:
            _scheduler.mark_superadmins()
        user_conns.add(conn_id)
    else:
        user_conns = _presence_by_village[ticket_data.village_id][ticket_data.user_id]
        if not user_conns:
            _scheduler.mark_village(ticket_data.village_id)
        user_conns.add(conn_id)

    return conn_id


def unregister_connection(conn_id: uuid.UUID) -> None:
    conn = _presence_connections.pop(conn_id, None)
    if conn is None:
        return

    if conn.village_id is None:
        user_conns = _presence_superadmins.get(conn.user_id)
        if user_conns is None:
            return
        user_conns.discard(conn_id)
        if not user_conns:
            _presence_superadmins.pop(conn.user_id, None)
            _scheduler.mark_superadmins()
        return

    village_users = _presence_by_village.get(conn.village_id)
    if village_users is None:
        return
    user_conns = village_users.get(conn.user_id)
    if user_conns is None:
        return

    user_conns.discard(conn_id)
    if not user_conns:
        village_users.pop(conn.user_id, None)
        _scheduler.mark_village(conn.village_id)
    if not village_users:
        _presence_by_village.pop(conn.village_id, None)
