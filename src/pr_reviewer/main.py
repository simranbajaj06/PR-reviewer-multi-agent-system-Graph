"""API process (composition root). Serves the GitHub webhook, the dashboard
and the REST API. It only ENQUEUES work; the actual reviewing happens in the
worker process (worker_main.py) unless RUN_EMBEDDED_WORKER=true.
"""
from dotenv import load_dotenv
load_dotenv()  # must run before GitHubClient/GroqReviewer read os.environ below

import logging
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from src.pr_reviewer.adapters.github.signature_verifier import GitHubSignatureVerifier
from src.pr_reviewer.adapters.persistence import db
from src.pr_reviewer.api.dashboard_router import router as dashboard_router
from src.pr_reviewer.api.webhook_router import router as webhook_router
from src.pr_reviewer.bootstrap import build_dedupe, build_graph, build_queue
from src.pr_reviewer.config import get_settings
from src.pr_reviewer.domain.services.webhook_handler import WebhookHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PR Reviewer Agent")
    db.init_db()  # creates the PostgreSQL tables on first run (safe to repeat)

    verifier = GitHubSignatureVerifier(settings.github_webhook_secret)
    review_queue = build_queue(settings)
    dedupe = build_dedupe(settings)

    # Graph services: used by the dashboard's graph explorer, and (when the
    # embedded worker is on) by the worker. None if the graph is disabled.
    graph = build_graph(settings)
    app.state.graph = graph

    app.state.webhook_handler = WebhookHandler(
        verifier, review_queue,
        index_on_push=graph is not None,
        index_branch=settings.index_branch,
        bot_username=settings.bot_username,
        dedupe=dedupe,
    )
    app.state.review_queue = review_queue

    app.include_router(webhook_router)
    app.include_router(dashboard_router)

    if settings.embedded_worker:
        # Dev convenience only. In production run worker_main.py as its own process.
        from src.pr_reviewer.adapters.github.github_client import GitHubClient
        from src.pr_reviewer.adapters.llm.grok_reviewer import GroqReviewer
        from src.pr_reviewer.workers.review_worker import run_worker

        threading.Thread(
            target=run_worker,
            args=(review_queue, GitHubClient(), GroqReviewer(graph_store=graph.store if graph else None),
                  graph.updater if graph else None),
            kwargs={"bot_username": settings.bot_username, "dedupe": dedupe},
            daemon=True,
        ).start()
        logger.info("Embedded worker thread started")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(Path(__file__).parent / "ui" / "index.html")

    @app.get("/health")
    def health():
        """Reports 503 when PostgreSQL or the queue is unreachable, so
        Docker / load balancers can restart or stop routing to a broken instance."""
        body: dict = {
            "status": "ok",
            "graph_enabled": graph is not None,
            "queue_backend": settings.queue_backend,
            "database": db.db_info(),
        }
        try:
            body["saved_reviews"] = db.get_stats()["total_reviews"]
        except Exception as exc:
            body.update(status="error", database_error=str(exc))
        if hasattr(review_queue, "depth"):
            try:
                body["queue"] = review_queue.depth()
            except Exception as exc:
                body.update(status="error", queue_error=str(exc))
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    return app


app = create_app()


@app.on_event("shutdown")
def _shutdown():
    graph = app.state.graph
    if graph is not None and hasattr(graph.store, "close"):
        graph.store.close()
    db.close_pool()
