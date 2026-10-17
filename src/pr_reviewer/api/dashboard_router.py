"""Read-only REST endpoints for the review dashboard (see ui/index.html).

  /api/reviews, /api/reviews/{id}, /api/stats, /api/repos   -> SQLite review history
  /api/graph/*                                              -> the FalkorDB code graph
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.pr_reviewer.adapters.persistence.db import get_review, get_stats, list_reviews

router = APIRouter(prefix="/api")


@router.get("/reviews")
def get_reviews(
    repo: str | None = Query(default=None, description="Filter by repo full name"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return {"reviews": list_reviews(repo=repo, limit=limit)}


@router.get("/reviews/{review_id}")
def get_review_detail(review_id: int) -> dict[str, Any]:
    review = get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"review": review}


@router.get("/stats")
def dashboard_stats() -> dict[str, Any]:
    return {"stats": get_stats()}


@router.get("/repos")
def list_repos() -> dict[str, Any]:
    return {"repos": get_stats()["repos"]}


# ---- code graph explorer -------------------------------------------------------
def _store(request: Request):
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="Graph is disabled")
    return graph.store


def _guard(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as exc:  # missing graph, FalkorDB down, ...
        raise HTTPException(status_code=404, detail=f"Graph unavailable: {exc}")


@router.get("/graph/repos")
def graph_repos(request: Request) -> dict[str, Any]:
    store = _store(request)
    return {"repos": _guard(store.list_repos)}


@router.get("/graph/overview")
def graph_overview(request: Request, repo: str) -> dict[str, Any]:
    store = _store(request)
    return _guard(lambda: store.overview(repo))


@router.get("/graph/search")
def graph_search(request: Request, repo: str, q: str = Query(min_length=1)) -> dict[str, Any]:
    store = _store(request)
    return {"results": _guard(lambda: store.search_functions(repo, q))}


@router.get("/graph/function")
def graph_function(request: Request, repo: str, id: str) -> dict[str, Any]:
    store = _store(request)
    view = _guard(lambda: store.function_view(repo, id))
    if view is None:
        raise HTTPException(status_code=404, detail="Function not found")
    return view
