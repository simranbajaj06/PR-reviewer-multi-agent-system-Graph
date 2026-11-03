"""Turns GitHub's `pull_request_review_comment` webhook JSON into our domain model."""
from typing import Any

from src.pr_reviewer.domain.models import ReviewCommentEvent


def parse_review_comment_event(payload: dict[str, Any]) -> ReviewCommentEvent:
    c = payload["comment"]
    pr = payload["pull_request"]
    user = c.get("user") or {}
    return ReviewCommentEvent(
        action=payload["action"],
        repo_full_name=payload["repository"]["full_name"],
        pr_number=pr["number"],
        pr_title=pr.get("title", ""),
        head_sha=(pr.get("head") or {}).get("sha", ""),
        comment_id=c["id"],
        in_reply_to_id=c.get("in_reply_to_id"),
        path=c.get("path", ""),
        line=c.get("line") or c.get("original_line"),
        diff_hunk=c.get("diff_hunk", ""),
        body=c.get("body", ""),
        author=user.get("login", ""),
        author_type=user.get("type", ""),
    )
