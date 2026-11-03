"""Production queue: Redis Streams + a consumer group.

Why this instead of the in-memory queue:
  * jobs survive an API/worker restart (Redis persists them, AOF is on in compose)
  * several worker processes can share the load - each job goes to exactly one
  * a job is only removed after the worker acks it. If a worker crashes mid-job,
    the job stays "pending" and another worker re-claims it after
    `visibility_timeout_s`
  * a job that keeps failing is moved to a dead-letter stream after
    `max_attempts` deliveries instead of looping forever

Stream layout:
  pr_reviewer:jobs        live jobs
  pr_reviewer:jobs:dead   jobs that exhausted their attempts (inspect with XRANGE)
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any, cast

import redis

from src.pr_reviewer.adapters.queue.serialization import decode_job, encode_job
from src.pr_reviewer.domain.interfaces import ReviewQueue
from src.pr_reviewer.domain.models import CommentQuery, IndexJob, PullRequestEvent, QueueJob

logger = logging.getLogger(__name__)


class RedisReviewQueue(ReviewQueue):
    def __init__(
        self,
        url: str,
        stream: str = "pr_reviewer:jobs",
        group: str = "workers",
        consumer: str | None = None,
        visibility_timeout_s: int = 900,   # must be longer than your slowest job
        max_attempts: int = 3,
        block_ms: int = 2000,
    ):
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._stream = stream
        self._dead = f"{stream}:dead"
        self._group = group
        self._consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self._visibility_ms = visibility_timeout_s * 1000
        self._max_attempts = max_attempts
        self._block_ms = block_ms
        self._inflight: dict[int, str] = {}      # id(job object) -> stream entry id
        self._lock = threading.Lock()
        self._ensure_group()

    # ---- setup / health ---------------------------------------------------
    def _ensure_group(self) -> None:
        try:
            self._r.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):      # group already exists = fine
                raise

    def ping(self) -> bool:
        return bool(self._r.ping())

    def depth(self) -> dict[str, int]:
        """Rough numbers for /health and dashboards."""
        pending = self._r.xpending(self._stream, self._group)["pending"]
        return {
            "stored": self._r.xlen(self._stream),
            "in_progress_or_retrying": pending,
            "dead_lettered": self._r.xlen(self._dead),
        }

    # ---- producer -----------------------------------------------------------
    def enqueue(self, job: QueueJob) -> None:
        self._r.xadd(self._stream, {"job": encode_job(job)})
        if isinstance(job, PullRequestEvent):
            logger.info("[queue] enqueued PR #%s (%s)", job.pr_number, job.repo_full_name)
        elif isinstance(job, IndexJob):
            logger.info("[queue] enqueued index job (%s @ %s)", job.repo_full_name, job.sha[:7])
        elif isinstance(job, CommentQuery):
            logger.info("[queue] enqueued comment query on PR #%s (%s)",
                        job.event.pr_number, job.event.repo_full_name)

    # ---- consumer -------------------------------------------------------------
    def dequeue(self) -> QueueJob | None:
        # 1) Jobs whose previous worker died / timed out come first.
        job = self._reclaim_stale()
        if job is not None:
            return job

        # 2) Otherwise wait (long-poll) for a brand-new job.
        # redis-py's type hints for stream calls are very loose, so we state the
        # real shape ourselves: [[stream_name, [(entry_id, {field: value}), ...]]]
        resp = cast("list[Any]", self._r.xreadgroup(
            self._group, self._consumer, {self._stream: ">"}, count=1, block=self._block_ms
        ))
        if not resp:
            return None
        entry_id, fields = cast("tuple[str, dict[str, str] | None]", resp[0][1][0])
        return self._to_job(entry_id, fields)

    def _reclaim_stale(self) -> QueueJob | None:
        res = cast("tuple[Any, ...]", self._r.xautoclaim(
            self._stream, self._group, self._consumer,
            min_idle_time=self._visibility_ms, start_id="0-0", count=1,
        ))
        # (next_id, messages[, deleted_ids]) depending on the Redis version
        messages = cast("list[tuple[str, dict[str, str] | None]]", res[1])
        if not messages:
            return None
        entry_id, fields = messages[0]

        info = self._r.xpending_range(self._stream, self._group, min=entry_id, max=entry_id, count=1)
        attempts = info[0]["times_delivered"] if info else 1
        if attempts > self._max_attempts:
            self._dead_letter(entry_id, fields, f"gave up after {attempts - 1} attempts")
            return None
        logger.warning("[queue] retrying job %s (attempt %s/%s)", entry_id, attempts, self._max_attempts)
        return self._to_job(entry_id, fields)

    def _to_job(self, entry_id: str, fields: dict[str, str] | None) -> QueueJob | None:
        if fields is None:   # entry was trimmed/deleted while pending
            self._r.xack(self._stream, self._group, entry_id)
            return None
        try:
            job = decode_job(fields["job"])
        except Exception as exc:                       # poison message: never retry it
            self._dead_letter(entry_id, fields, f"undecodable: {exc}")
            return None
        with self._lock:
            self._inflight[id(job)] = entry_id
        return job

    def _dead_letter(self, entry_id: str, fields: dict[str, str] | None, reason: str) -> None:
        logger.error("[queue] dead-lettering %s: %s", entry_id, reason)
        pipe = self._r.pipeline()
        pipe.xadd(self._dead, {"job": (fields or {}).get("job", ""), "reason": reason, "orig_id": entry_id})
        pipe.xack(self._stream, self._group, entry_id)
        pipe.xdel(self._stream, entry_id)
        pipe.execute()

    def ack(self, job: QueueJob) -> None:
        with self._lock:
            entry_id = self._inflight.pop(id(job), None)
        if entry_id:
            pipe = self._r.pipeline()
            pipe.xack(self._stream, self._group, entry_id)
            pipe.xdel(self._stream, entry_id)      # streams keep entries after ack; remove it
            pipe.execute()

    def fail(self, job: QueueJob) -> None:
        # Leave it pending: after the visibility timeout dequeue() hands it out
        # again (a natural retry delay) until max_attempts, then it is dead-lettered.
        with self._lock:
            self._inflight.pop(id(job), None)