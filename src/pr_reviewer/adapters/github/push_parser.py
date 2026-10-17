"""Turns a GitHub `push` webhook payload into an IndexJob, or explains why
the push isn't worth indexing. Lives next to payload_parser.py
(src/pr_reviewer/adapters/github/push_parser.py).

Every non-delete branch push triggers a graph rebuild:
  * "Index branch" push  -> graph key is ``owner__repo``  (used by PR reviews)
  * Any other branch     -> graph key is ``owner__repo__branch`` (isolated)

The "index branch" is determined in this order:
  1. ``index_branch`` arg (from INDEX_BRANCH env var) — explicit override
  2. ``repository.default_branch`` from the GitHub payload — automatic
"""
from src.pr_reviewer.domain.models import IndexJob

_ZERO_SHA = "0" * 40
_REF_PREFIX = "refs/heads/"


def parse_push_event(
    payload: dict,
    index_branch: str | None = None,
) -> tuple[IndexJob | None, str | None]:
    """Returns (IndexJob, None) to index, or (None, reason) to skip.

    Args:
        payload:      Raw GitHub push webhook payload dict.
        index_branch: Override which branch is treated as the integration branch.
                      When None, falls back to payload's ``repository.default_branch``.
    """
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name")
    ref = payload.get("ref", "")
    after = payload.get("after", "")

    if not full_name:
        return None, "malformed push payload: missing repository.full_name"
    if payload.get("deleted") or after == _ZERO_SHA:
        return None, "branch deleted — nothing to index"
    if not ref.startswith(_REF_PREFIX):
        return None, f"ref '{ref}' is not a branch push (tags are skipped)"
    if not after:
        return None, "push has no head commit"

    branch = ref[len(_REF_PREFIX):]   # e.g. "main" or "feature/my-branch"

    # Decide which branch is the "primary" one whose pushes update the
    # default-branch graph that PR reviews read from.
    # Priority: explicit INDEX_BRANCH env var > GitHub's default_branch field.
    primary = index_branch or repo.get("default_branch", "main")
    is_primary = (branch == primary)

    return IndexJob(
        repo_full_name=full_name,
        sha=after,
        # Empty branch = "this IS the primary/default graph" (backward compat).
        # Non-empty   = isolated branch graph, never clobbers the primary graph.
        branch="" if is_primary else branch,
    ), None