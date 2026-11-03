"""Worker process: pulls jobs from the queue and runs the review agents.

    python -m src.pr_reviewer.worker_main

Run it as its own container/process (see docker-compose.yml). You can run
several copies; Redis hands each job to exactly one of them.
"""
from dotenv import load_dotenv
load_dotenv()

import logging
import signal
import threading

from src.pr_reviewer.adapters.github.github_client import GitHubClient
from src.pr_reviewer.adapters.llm.grok_reviewer import GroqReviewer
from src.pr_reviewer.adapters.persistence import db
from src.pr_reviewer.bootstrap import build_dedupe, build_graph, build_queue
from src.pr_reviewer.config import get_settings
from src.pr_reviewer.workers.review_worker import run_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker_main")


def main() -> None:
    settings = get_settings()
    if settings.queue_backend == "memory":
        raise SystemExit("A separate worker needs QUEUE_BACKEND=redis (the memory queue is private to one process).")

    db.init_db()
    queue = build_queue(settings)
    dedupe = build_dedupe(settings)
    graph = build_graph(settings)

    stop = threading.Event()

    def _graceful(signum, _frame):
        logger.info("signal %s received: finishing the current job, then exiting", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _graceful)   # docker stop / deploy
    signal.signal(signal.SIGINT, _graceful)    # Ctrl+C

    run_worker(
        queue,
        GitHubClient(),
        GroqReviewer(graph_store=graph.store if graph else None),
        graph.updater if graph else None,
        poll_interval=0,                 # the Redis queue long-polls
        bot_username=settings.bot_username,
        dedupe=dedupe,
        stop_event=stop,
    )

    if graph is not None and hasattr(graph.store, "close"):
        graph.store.close()
    db.close_pool()


if __name__ == "__main__":
    main()
