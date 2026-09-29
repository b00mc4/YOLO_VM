from __future__ import annotations
import asyncio
import enum
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from app.core.config import get_settings
from app.core.error_messages import RealtimeErrors
from app.core.sse_channel import CLOSE_SENTINEL, StreamIdentity

settings = get_settings()

FORCE_CLOSE_EVENT = "force_close"
_MIN_QUEUE_SIZE = 2


class StreamCloseCode(str, enum.Enum):
    STREAM_LIMIT = "STREAM_LIMIT"
    SESSION_REVOKED = "SESSION_REVOKED"


@dataclass(slots=True, eq=False)
class StreamHandle:
    identity: StreamIdentity
    queue: asyncio.Queue
    stream_id: uuid.UUID = field(default_factory=uuid.uuid4)


def _close_detail(code: StreamCloseCode, max_per_session: int) -> str:
    if code is StreamCloseCode.STREAM_LIMIT:
        return RealtimeErrors.stream_limit_reached(max_per_session)
    return RealtimeErrors.SESSION_REVOKED


def _signal_close(queue: asyncio.Queue, code: StreamCloseCode, detail: str) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    queue.put_nowait({"event": FORCE_CLOSE_EVENT, "data": {"code": code.value, "detail": detail}})
    queue.put_nowait(CLOSE_SENTINEL)


class StreamRegistry:
    def __init__(self, queue_maxsize: int, max_per_session: int) -> None:
        self._queue_maxsize = max(queue_maxsize, _MIN_QUEUE_SIZE)
        self._max_per_session = max_per_session
        self._streams: dict[uuid.UUID, StreamHandle] = {}
        self._by_session: dict[uuid.UUID, deque[StreamHandle]] = {}

    def open(self, identity: StreamIdentity) -> StreamHandle:
        handle = StreamHandle(identity=identity, queue=asyncio.Queue(maxsize=self._queue_maxsize))
        self._streams[handle.stream_id] = handle
        session_streams = self._by_session.setdefault(identity.session_id, deque())
        session_streams.append(handle)

        while len(session_streams) > self._max_per_session:
            evicted = session_streams.popleft()
            self._streams.pop(evicted.stream_id, None)
            self._close(evicted, StreamCloseCode.STREAM_LIMIT)

        return handle

    def close(self, handle: StreamHandle) -> None:
        self._detach(handle)

    def revoke(self, stream_ids: Iterable[uuid.UUID], code: StreamCloseCode) -> int:
        revoked = 0
        for stream_id in stream_ids:
            handle = self._streams.get(stream_id)
            if handle is None:
                continue
            self._detach(handle)
            self._close(handle, code)
            revoked += 1
        return revoked

    def revoke_sessions(self, session_ids: Iterable[uuid.UUID]) -> int:
        revoked = 0
        for session_id in session_ids:
            for handle in self._by_session.pop(session_id, ()):
                self._streams.pop(handle.stream_id, None)
                self._close(handle, StreamCloseCode.SESSION_REVOKED)
                revoked += 1
        return revoked

    def snapshot(self) -> list[StreamHandle]:
        return list(self._streams.values())

    def _close(self, handle: StreamHandle, code: StreamCloseCode) -> None:
        _signal_close(handle.queue, code, _close_detail(code, self._max_per_session))

    def _detach(self, handle: StreamHandle) -> None:
        self._streams.pop(handle.stream_id, None)
        session_id = handle.identity.session_id
        session_streams = self._by_session.get(session_id)
        if session_streams is None:
            return
        try:
            session_streams.remove(handle)
        except ValueError:
            pass
        if not session_streams:
            self._by_session.pop(session_id, None)


stream_registry = StreamRegistry(
    queue_maxsize=settings.channel_queue_size,
    max_per_session=settings.sse_max_streams_per_session,
)
