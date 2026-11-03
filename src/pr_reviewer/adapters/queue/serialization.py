"""Turns queue jobs (frozen dataclasses) into JSON strings and back, so they
can travel through Redis."""
from __future__ import annotations

import json
from dataclasses import asdict

from src.pr_reviewer.domain.models import (
    CommentQuery,
    IndexJob,
    PullRequestEvent,
    QueueJob,
    ReviewCommentEvent,
)


def encode_job(job: QueueJob) -> str:
    return json.dumps({"type": type(job).__name__, "data": asdict(job)})


def decode_job(raw: str) -> QueueJob:
    obj = json.loads(raw)
    kind, data = obj["type"], obj["data"]
    if kind == "PullRequestEvent":
        return PullRequestEvent(**data)
    if kind == "IndexJob":
        return IndexJob(**data)
    if kind == "CommentQuery":
        return CommentQuery(event=ReviewCommentEvent(**data["event"]), question=data["question"])
    raise ValueError(f"unknown job type in queue: {kind!r}")
