"""Builders shared by the API process (main.py) and the worker process
(worker_main.py), so both wire the same adapters from the same settings."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from src.pr_reviewer.config import Settings
from src.pr_reviewer.domain.interfaces import DedupeStore, ReviewQueue
from src.pr_reviewer.graph.ports import RepoSource
from src.pr_reviewer.graph.store import GraphStore
from src.pr_reviewer.graph.updater import GraphUpdater

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphServices:
    """Everything the graph layer exposes to the rest of the app."""
    store: GraphStore
    source: RepoSource
    updater: GraphUpdater


def build_graph(settings: Settings) -> GraphServices | None:
    """Builds the graph services once at startup. Returns None when the graph
    is not configured, so the app still works with diff-only reviews."""
    if not settings.graph_enabled:
        logger.warning("Graph DB not configured; graph disabled, reviews will use the diff only")
        return None

    from src.pr_reviewer.adapters.github.github_repo_fetcher import GitHubSource

    # FalkorDB may still be starting (containers boot in parallel). Wait for it
    # instead of giving up on the first failed ping, which would leave this
    # process on diff-only reviews until the next restart.
    attempts = max(1, int(os.getenv("GRAPH_CONNECT_RETRIES", "10")))
    delay = float(os.getenv("GRAPH_CONNECT_DELAY", "3"))
    store = None
    for attempt in range(1, attempts + 1):
        try:
            store = GraphStore(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                password=settings.falkordb_password,
                fixed_graph_name=settings.graph_name,
            )
            store.ping()
            break
        except Exception as exc:
            store = None
            if attempt == attempts:
                logger.warning("FalkorDB unreachable after %d attempts (%s); graph disabled, "
                               "reviews will use the diff only", attempts, exc)
            else:
                logger.info("FalkorDB not ready (%s); retry %d/%d in %.0fs", exc, attempt, attempts, delay)
                time.sleep(delay)
    if store is None:
        return None

    source = GitHubSource()  # reads GITHUB_TOKEN from the environment
    updater = GraphUpdater(source, store)
    logger.info("Graph enabled")
    return GraphServices(store=store, source=source, updater=updater)


def build_queue(settings: Settings) -> ReviewQueue:
    if settings.queue_backend == "memory":
        from src.pr_reviewer.adapters.queue.in_memory_queue import InMemoryReviewQueue
        logger.warning("Using the IN-MEMORY queue: single process only, jobs are lost on restart")
        return InMemoryReviewQueue()

    from src.pr_reviewer.adapters.queue.redis_queue import RedisReviewQueue
    queue = RedisReviewQueue(
        settings.redis_url,
        visibility_timeout_s=settings.queue_visibility_timeout_s,
        max_attempts=settings.queue_max_attempts,
    )
    queue.ping()   # fail fast at startup if Redis is wrong
    logger.info("Using the Redis queue")
    return queue


def build_dedupe(settings: Settings) -> DedupeStore:
    if settings.queue_backend == "memory":
        from src.pr_reviewer.adapters.dedupe.in_memory_dedupe import InMemoryDedupeStore
        return InMemoryDedupeStore()

    from src.pr_reviewer.adapters.dedupe.redis_dedupe import RedisDedupeStore
    return RedisDedupeStore(settings.redis_url)
