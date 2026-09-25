from __future__ import annotations
import uuid


class ConnectionLimitExceeded(Exception):
    def __init__(self, max_connections: int):
        self.max_connections = max_connections
        super().__init__("Too many concurrent connections for this user")


class InMemoryConnectionLimiter:
    def __init__(self) -> None:
        self._counts: dict[uuid.UUID, int] = {}

    def register(self, user_id: uuid.UUID, limit: int) -> None:
        current = self._counts.get(user_id, 0)
        if current >= limit:
            raise ConnectionLimitExceeded(max_connections=limit)
        self._counts[user_id] = current + 1

    def unregister(self, user_id: uuid.UUID) -> None:
        current = self._counts.get(user_id)
        if current is None:
            return
        if current <= 1:
            self._counts.pop(user_id, None)
        else:
            self._counts[user_id] = current - 1

    def get_count(self, user_id: uuid.UUID) -> int:
        return self._counts.get(user_id, 0)