import uuid
import time
import threading
from collections import deque

_DEFAULT_MAX_SESSIONS = 5
_GRACE_SECONDS = 30.0

class SessionManager:
    def __init__(self, max_sessions: int = _DEFAULT_MAX_SESSIONS):
        self.max_sessions = max_sessions
        self._sessions: dict[uuid.UUID, deque[str]] = {}
        self._graced: dict[str, tuple[uuid.UUID, float]] = {}
        self._lock = threading.Lock()

    def _cleanup_graced(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, (_, exp) in self._graced.items() if now >= exp]
        for sid in expired:
            del self._graced[sid]

    def add_session(self, user_id: uuid.UUID, session_id: str) -> None:
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = deque()
            sessions = self._sessions[user_id]
            if session_id in sessions:
                sessions.remove(session_id)
            sessions.append(session_id)
            while len(sessions) > self.max_sessions:
                evicted = sessions.popleft()
                self._graced[evicted] = (user_id, time.monotonic() + _GRACE_SECONDS)

    def is_valid_session(self, user_id: uuid.UUID, session_id: str) -> bool:
        with self._lock:
            sessions = self._sessions.get(user_id)
            if sessions and session_id in sessions:
                return True
            self._cleanup_graced()
            graced = self._graced.get(session_id)
            return graced is not None and graced[0] == user_id

    def remove_session_by_id(self, session_id: str) -> None:
        with self._lock:
            for user_id, sessions in list(self._sessions.items()):
                if session_id in sessions:
                    sessions.remove(session_id)
                    if not sessions:
                        del self._sessions[user_id]
                    break

    def remove_session_with_grace(self, session_id: str, user_id: uuid.UUID | None = None, grace_seconds: float = _GRACE_SECONDS) -> None:
        with self._lock:
            found_user_id = user_id
            for uid, sessions in list(self._sessions.items()):
                if session_id in sessions:
                    if found_user_id is None:
                        found_user_id = uid
                    sessions.remove(session_id)
                    if not sessions:
                        del self._sessions[uid]
                    break
            if found_user_id is not None:
                self._graced[session_id] = (found_user_id, time.monotonic() + grace_seconds)
            self._cleanup_graced()

    def remove_all_sessions(self, user_id: uuid.UUID) -> None:
        with self._lock:
            if user_id in self._sessions:
                del self._sessions[user_id]

session_manager = SessionManager(max_sessions=_DEFAULT_MAX_SESSIONS)
