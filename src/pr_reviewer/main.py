"""Composition root. This is the only file that imports both the abstract
interfaces AND the concrete adapter classes, and wires them together.
Everything else in the app depends on interfaces only.
"""
from dotenv import load_dotenv
load_dotenv()  # must run before GitHubClient/GroqReviewer read os.environ below

import logging
import threading
from dataclasses import dataclass

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from src.pr_reviewer.adapters.github.signature_verifier import GitHubSignatureVerifier
from src.pr_reviewer.adapters.queue.in_memory_queue import InMemoryReviewQueue
from src.pr_reviewer.adapters.persistence.db import db_path, get_stats, init_db
from src.pr_reviewer.api.dashboard_router import router as dashboard_router
from src.pr_reviewer.api.webhook_router import router as webhook_router
from src.pr_reviewer.config import Settings, get_settings
from src.pr_reviewer.domain.services.webhook_handler import WebhookHandler
from src.pr_reviewer.adapters.github.github_client import GitHubClient
from src.pr_reviewer.adapters.llm.grok_reviewer import GroqReviewer
from src.pr_reviewer.workers.review_worker import run_worker
from src.pr_reviewer.graph.ports import RepoSource
from src.pr_reviewer.graph.store import GraphStore
from src.pr_reviewer.graph.updater import GraphUpdater

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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

    try:
        store = GraphStore(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password,
            fixed_graph_name=settings.graph_name,
        )
        store.ping()
    except Exception as exc:
        logger.warning("FalkorDB unreachable (%s); graph disabled, reviews will use the diff only", exc)
        return None

    source = GitHubSource()  # reads GITHUB_TOKEN from the environment
    updater = GraphUpdater(source, store)

    logger.info("Graph enabled")
    return GraphServices(store=store, source=source, updater=updater)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PR Reviewer Agent")
    init_db()  # creates data/reviews.db tables on first run

    verifier = GitHubSignatureVerifier(settings.github_webhook_secret)
    review_queue = InMemoryReviewQueue()

    # Graph services: used by the worker for push syncs / first-PR bootstrap,
    # and by the reviewer for context. None if the graph is disabled.
    graph = build_graph(settings)
    app.state.graph = graph

    app.state.webhook_handler = WebhookHandler(
        verifier, review_queue,
        index_on_push=graph is not None,
        index_branch=settings.index_branch,
    )
    app.state.review_queue = review_queue

    app.include_router(webhook_router)
    app.include_router(dashboard_router)
    vcs_client = GitHubClient()
    reviewer = GroqReviewer(graph_store=graph.store if graph else None)

    worker_thread = threading.Thread(
        target=run_worker,
        args=(review_queue, vcs_client, reviewer, graph.updater if graph else None),
        daemon=True,
    )
    worker_thread.start()

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(Path(__file__).parent / "ui" / "index.html")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "graph_enabled": graph is not None,
            "db_file": db_path(),
            "saved_reviews": get_stats()["total_reviews"],
        }

    return app


app = create_app()


@app.on_event("shutdown")
def _shutdown():
    graph = app.state.graph
    if graph is not None and hasattr(graph.store, "close"):
        graph.store.close()