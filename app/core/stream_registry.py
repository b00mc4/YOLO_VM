from __future__ import annotations
import asyncio
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from app.core.config import get_settings
from app.core.sse_channel import CLOSE_SENTINEL

settings = get_settings()

FORCE_CLOSE_EVENT = "force_close"
_EVICTED_REASON = "Too many connections"
_MIN_QUEUE_SIZE = 2


@dataclass(slots=True, eq=False)
class StreamHandle:
    user_id: uuid.UUID
    village_id: uuid.UUID | None
    password_changed_at: datetime | None
    queue: asyncio.Queue
    stream_id: uuid.UUID = field(default_factory=uuid.uuid4)


def _signal_close(queue: asyncio.Queue, reason: str | None) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    if reason is not None:
        queue.put_nowait({"event": FORCE_CLOSE_EVENT, "data": reason})
    queue.put_nowait(CLOSE_SENTINEL)


class StreamRegistry:
    def __init__(self, queue_maxsize: int) -> None:
        self._queue_maxsize = max(queue_maxsize, _MIN_QUEUE_SIZE)
        self._streams: dict[uuid.UUID, StreamHandle] = {}
        self._by_user: dict[uuid.UUID, deque[StreamHandle]] = defaultdict(deque)

    def open(
        self,
        user_id: uuid.UUID,
        village_id: uuid.UUID | None,
        password_changed_at: datetime | None,
        max_per_user: int,
    ) -> StreamHandle:
        handle = StreamHandle(
            user_id=user_id,
            village_id=village_id,
            password_changed_at=password_changed_at,
            queue=asyncio.Queue(maxsize=self._queue_maxsize),
        )
        self._streams[handle.stream_id] = handle
        user_streams = self._by_user[user_id]
        user_streams.append(handle)

        while len(user_streams) > max_per_user:
            evicted = user_streams.popleft()
            self._streams.pop(evicted.stream_id, None)
            _signal_close(evicted.queue, _EVICTED_REASON)

        return handle

    def close(self, handle: StreamHandle) -> None:
        self._detach(handle)

    def revoke(self, stream_ids: Iterable[uuid.UUID], reason: str | None = None) -> int:
        revoked = 0
        for stream_id in stream_ids:
            handle = self._streams.get(stream_id)
            if handle is None:
                continue
            self._detach(handle)
            _signal_close(handle.queue, reason)
            revoked += 1
        return revoked

    def snapshot(self) -> list[StreamHandle]:
        return list(self._streams.values())

    def _detach(self, handle: StreamHandle) -> None:
        self._streams.pop(handle.stream_id, None)
        user_streams = self._by_user.get(handle.user_id)
        if user_streams is None:
            return
        try:
            user_streams.remove(handle)
        except ValueError:
            pass
        if not user_streams:
            self._by_user.pop(handle.user_id, None)


stream_registry = StreamRegistry(queue_maxsize=settings.channel_queue_size)
