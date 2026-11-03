"""Redis-backed DedupeStore: shared by every API/worker process and kept
across restarts. Uses SET NX EX, which is atomic."""
import redis

from src.pr_reviewer.domain.interfaces import DedupeStore


class RedisDedupeStore(DedupeStore):
    def __init__(self, url: str, prefix: str = "pr_reviewer:dedupe:"):
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._p = prefix

    def claim(self, key: str, ttl_seconds: int) -> bool:
        return bool(self._r.set(self._p + key, "1", nx=True, ex=ttl_seconds))

    def release(self, key: str) -> None:
        self._r.delete(self._p + key)

    def seen(self, key: str) -> bool:
        return bool(self._r.exists(self._p + key))

    def mark(self, key: str, ttl_seconds: int) -> None:
        self._r.set(self._p + key, "1", ex=ttl_seconds)
