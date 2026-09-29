from __future__ import annotations
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from app.core.config import get_settings

settings = get_settings()


@dataclass(frozen=True, slots=True)
class RotatedRefreshToken:
    user_id: uuid.UUID
    session_id: uuid.UUID
    rotated_at: float

    def is_within_grace(self, grace_seconds: float) -> bool:
        return time.monotonic() - self.rotated_at <= grace_seconds


class SessionManager:
    def __init__(self, max_sessions: int) -> None:
        self.max_sessions = max_sessions
        self._sessions_by_user: dict[uuid.UUID, deque[uuid.UUID]] = {}
        self._owner_by_session: dict[uuid.UUID, uuid.UUID] = {}
        self._rotated_by_hash: dict[str, RotatedRefreshToken] = {}
        self._rotated_hash_by_session: dict[uuid.UUID, str] = {}
        self._lock = threading.Lock()

    def add_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> list[uuid.UUID]:
        with self._lock:
            sessions = self._sessions_by_user.setdefault(user_id, deque())
            if session_id in sessions:
                sessions.remove(session_id)
            sessions.append(session_id)
            self._owner_by_session[session_id] = user_id

            evicted: list[uuid.UUID] = []
            while len(sessions) > self.max_sessions:
                evicted_id = sessions.popleft()
                self._forget(evicted_id)
                evicted.append(evicted_id)
            return evicted

    def is_valid_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        with self._lock:
            return self._owner_by_session.get(session_id) == user_id

    def remove_session(self, session_id: uuid.UUID) -> None:
        with self._lock:
            user_id = self._owner_by_session.get(session_id)
            if user_id is not None:
                self._detach(user_id, session_id)
            self._forget(session_id)

    def remove_all_sessions(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        with self._lock:
            sessions = self._sessions_by_user.pop(user_id, deque())
            for session_id in sessions:
                self._forget(session_id)
            return list(sessions)

    def record_rotation(self, old_token_hash: str, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        with self._lock:
            self._forget_rotation(session_id)
            self._rotated_by_hash[old_token_hash] = RotatedRefreshToken(
                user_id=user_id,
                session_id=session_id,
                rotated_at=time.monotonic(),
            )
            self._rotated_hash_by_session[session_id] = old_token_hash

    def find_rotation(self, token_hash: str) -> RotatedRefreshToken | None:
        with self._lock:
            return self._rotated_by_hash.get(token_hash)

    def _detach(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        sessions = self._sessions_by_user.get(user_id)
        if sessions is None:
            return
        try:
            sessions.remove(session_id)
        except ValueError:
            pass
        if not sessions:
            self._sessions_by_user.pop(user_id, None)

    def _forget(self, session_id: uuid.UUID) -> None:
        self._owner_by_session.pop(session_id, None)
        self._forget_rotation(session_id)

    def _forget_rotation(self, session_id: uuid.UUID) -> None:
        token_hash = self._rotated_hash_by_session.pop(session_id, None)
        if token_hash is not None:
            self._rotated_by_hash.pop(token_hash, None)


session_manager = SessionManager(max_sessions=settings.auth_max_sessions_per_user)
