"""Single-process DedupeStore with expiry. For local dev and unit tests only."""
import threading
import time

from src.pr_reviewer.domain.interfaces import DedupeStore


class InMemoryDedupeStore(DedupeStore):
    def __init__(self):
        self._items: dict[str, float] = {}      # key -> expiry (monotonic seconds)
        self._lock = threading.Lock()

    def _live(self, key: str) -> bool:
        exp = self._items.get(key)
        if exp is None:
            return False
        if exp < time.monotonic():
            del self._items[key]
            return False
        return True

    def claim(self, key: str, ttl_seconds: int) -> bool:
        with self._lock:
            if self._live(key):
                return False
            self._items[key] = time.monotonic() + ttl_seconds
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def seen(self, key: str) -> bool:
        with self._lock:
            return self._live(key)

    def mark(self, key: str, ttl_seconds: int) -> None:
        with self._lock:
            self._items[key] = time.monotonic() + ttl_seconds
