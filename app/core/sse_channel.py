from __future__ import annotations
import asyncio
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from fastapi import HTTPException, status
from app.core.security import generate_secure_token, hash_token
from app.models.user import UserRole
from app.core.error_messages import RealtimeErrors

CLOSE_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class StreamIdentity:
    user_id: uuid.UUID
    session_id: uuid.UUID
    village_id: uuid.UUID | None
    password_changed_at: datetime | None

    def belongs_to_same_session(self, other: StreamIdentity) -> bool:
        return self.user_id == other.user_id and self.session_id == other.session_id


def try_emit(queue: asyncio.Queue, item) -> bool:
    """
    ยัด item ลง queue แบบไม่บล็อก ถ้าเต็มให้เขี่ยของเก่าสุดออก 1 ที่แล้วปลูก
    CLOSE_SENTINEL แทนที่ เพื่อสื่อให้ผู้บริโภค queue รู้ว่าตามไม่ทันและควรปิด
    connection ทิ้ง คืนค่า False เมื่อ queue เต็ม (สัญญาณให้ผู้เรียกลบ queue
    นี้ออกจาก subscriber set ทันที), True เมื่อส่งสำเร็จตามปกติ
    """
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        pass

    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass

    try:
        queue.put_nowait(CLOSE_SENTINEL)
    except asyncio.QueueFull:
        pass

    return False


class SSEChannel:
    _SWEEP_INTERVAL_SECONDS = 60.0
    _MAX_TRACKED_TICKETS = 10_000

    CLOSE_SENTINEL = CLOSE_SENTINEL

    def __init__(self, ticket_expire_seconds: int) -> None:
        self._ticket_expire_seconds = ticket_expire_seconds
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)
        self._global_subscribers: set[asyncio.Queue] = set()
        self._tickets: OrderedDict[str, tuple[StreamIdentity, datetime]] = OrderedDict()
        self._last_sweep_at = monotonic()

    def _sweep_expired_tickets(self) -> None:
        now_monotonic = monotonic()
        if now_monotonic - self._last_sweep_at < self._SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep_at = now_monotonic

        now_utc = datetime.now(timezone.utc)
        expired_keys = [
            token_hash
            for token_hash, (_, expire_at) in self._tickets.items()
            if expire_at < now_utc
        ]
        for token_hash in expired_keys:
            self._tickets.pop(token_hash, None)

    def issue_ticket(self, identity: StreamIdentity) -> str:
        self._sweep_expired_tickets()

        if len(self._tickets) >= self._MAX_TRACKED_TICKETS:
            self._tickets.popitem(last=False)

        raw_token = generate_secure_token()
        expire_at = datetime.now(timezone.utc) + timedelta(seconds=self._ticket_expire_seconds)
        self._tickets[hash_token(raw_token)] = (identity, expire_at)
        return raw_token

    def resolve_ticket(self, raw_token: str) -> StreamIdentity:
        ticket = self._tickets.pop(hash_token(raw_token), None)
        if ticket is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=RealtimeErrors.INVALID_OR_EXPIRED_TICKET)

        identity, expire_at = ticket
        if expire_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=RealtimeErrors.INVALID_OR_EXPIRED_TICKET)

        return identity

    def subscribe(self, village_id: uuid.UUID | None, queue: asyncio.Queue) -> None:
        if village_id is None:
            self._global_subscribers.add(queue)
        else:
            self._subscribers[village_id].add(queue)

    def unsubscribe(self, village_id: uuid.UUID | None, queue: asyncio.Queue) -> None:
        if village_id is None:
            self._global_subscribers.discard(queue)
            return

        subscribers = self._subscribers.get(village_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(village_id, None)

    async def publish(self, village_id: uuid.UUID, event: str, data: dict) -> None:
        item = {"event": event, "data": data}

        subscribers = self._subscribers.get(village_id)
        if subscribers:
            dead = [queue for queue in list(subscribers) if not try_emit(queue, item)]
            for queue in dead:
                subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(village_id, None)


    async def publish_global(self, event: str, data: dict) -> None:
        if not self._global_subscribers:
            return

        item = {"event": event, "data": data}
        dead = [queue for queue in list(self._global_subscribers) if not try_emit(queue, item)]
        for queue in dead:
            self._global_subscribers.discard(queue)

class ChannelService:
    def __init__(self, ticket_expire_seconds: int) -> None:
        self._channel = SSEChannel(ticket_expire_seconds=ticket_expire_seconds)

    def issue_ticket(self, current_user, session_id: uuid.UUID) -> str:
        scope_village_id = (
            None if current_user.role == UserRole.SUPERADMIN else current_user.village_id
        )
        return self._channel.issue_ticket(
            StreamIdentity(
                user_id=current_user.id,
                session_id=session_id,
                village_id=scope_village_id,
                password_changed_at=current_user.password_changed_at,
            )
        )

    def resolve_ticket(self, raw_token: str) -> StreamIdentity:
        return self._channel.resolve_ticket(raw_token)

    def subscribe(self, village_id: uuid.UUID | None, queue: asyncio.Queue) -> None:
        self._channel.subscribe(village_id, queue)

    def unsubscribe(self, village_id: uuid.UUID | None, queue: asyncio.Queue) -> None:
        self._channel.unsubscribe(village_id, queue)

    async def publish(self, village_id: uuid.UUID, event: str, data: dict) -> None:
        await self._channel.publish(village_id, event, data)

    async def publish_global(self, event: str, data: dict) -> None:
        await self._channel.publish_global(event, data)