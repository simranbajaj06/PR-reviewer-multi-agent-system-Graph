"""Application configuration, loaded from environment variables."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()


@dataclass(frozen=True)
class Settings:
    github_webhook_secret: str
    # FalkorDB holds the code graph the reviewers get their context from.
    # If it is unreachable at startup the app still runs (diff-only reviews).
    graph_enabled: bool = True
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_password: str | None = None
    # Optional: use ONE fixed graph for every repo (e.g. the graph you built by hand
    # with `python -m src.pr_reviewer.graph.build_graph <repo> code_graph`).
    # Unset = one graph per repo, named "owner__name".
    graph_name: str | None = None
    # Override which branch is treated as the "integration" branch for graph updates.
    # When set, pushes to this branch update the default-branch graph (owner__repo)
    # regardless of what GitHub reports as `repository.default_branch`.
    # Example: set INDEX_BRANCH=develop when PRs merge into develop, not main.
    # Leave unset to use GitHub's own default_branch from each push payload.
    index_branch: str | None = None


def get_settings() -> Settings:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError(
            "GITHUB_WEBHOOK_SECRET is not set. Copy .env.example to .env and fill it in."
        )
    return Settings(
        github_webhook_secret=secret,
        graph_enabled=os.getenv("GRAPH_ENABLED", "true").lower() == "true",
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        falkordb_password=os.getenv("FALKORDB_PASSWORD") or None,
        graph_name=os.getenv("GRAPH_NAME") or None,
        index_branch=os.getenv("INDEX_BRANCH") or None,
    )