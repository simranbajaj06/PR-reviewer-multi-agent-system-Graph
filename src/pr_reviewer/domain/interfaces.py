"""Ports (abstract interfaces). Concrete implementations live under adapters/
and are wired in at the composition root (main.py) — this is the Dependency
Inversion piece of SOLID: the domain never imports a concrete adapter.
"""
from abc import ABC, abstractmethod

from src.pr_reviewer.domain.models import PullRequestEvent, QueueJob, Review, ReviewCommentEvent


class WebhookVerifier(ABC):
    """Confirms an inbound webhook request genuinely came from GitHub."""

    @abstractmethod
    def verify(self, payload_body: bytes, signature_header: str | None) -> bool:
        ...


class ReviewQueue(ABC):
    """Where work items (PR reviews, index jobs) go to be picked up later."""

    @abstractmethod
    def enqueue(self, job: QueueJob) -> None:
        ...

    @abstractmethod
    def dequeue(self) -> QueueJob | None:
        """Returns the next job, or None if the queue is currently empty.
        May block briefly (long-poll) while waiting for work."""
        ...

    # --- delivery acknowledgement (at-least-once queues override these) ---
    def ack(self, job: QueueJob) -> None:
        """The job was handled; never deliver it again. No-op for queues
        that forget a job as soon as it is dequeued."""

    def fail(self, job: QueueJob) -> None:
        """Handling raised an unexpected error. Queues that support retries
        make the job available again (after a delay) or dead-letter it."""


class DedupeStore(ABC):
    """Shared 'have I seen this key?' memory with expiry. Replaces the
    per-process sets/dicts so several API/worker processes agree."""

    @abstractmethod
    def claim(self, key: str, ttl_seconds: int) -> bool:
        """Atomically remember `key`. True if it was new (caller owns it),
        False if someone already claimed it."""
        ...

    @abstractmethod
    def release(self, key: str) -> None:
        """Forget `key` (used to undo a claim when the work failed)."""
        ...

    @abstractmethod
    def seen(self, key: str) -> bool:
        ...

    @abstractmethod
    def mark(self, key: str, ttl_seconds: int) -> None:
        ...


class VCSClient(ABC):
    """Fetches a pull request's content and posts results back to the host (GitHub, etc.)."""

    @abstractmethod
    def fetch_diff(self, event: PullRequestEvent) -> str:
        ...

    @abstractmethod
    def post_comment(self, event: PullRequestEvent, body: str) -> None:
        """Posts a general (non-inline) comment on the pull request."""
        ...

    @abstractmethod
    def post_review(
        self,
        event: PullRequestEvent,
        body: str,
        findings: list[dict],
        verdict: str = "COMMENT",
    ) -> None:
        """Posts a proper pull-request review with inline line-level comments."""
        ...

    @abstractmethod
    def fetch_review_thread(self, repo_full_name: str, pr_number: int, root_comment_id: int) -> list[dict]:
        """All comments of one inline thread, oldest first. Each dict has:
        id, author, author_type, body, path, line, diff_hunk."""
        ...

    @abstractmethod
    def reply_to_review_comment(
        self, repo_full_name: str, pr_number: int, root_comment_id: int, body: str
    ) -> None:
        """Posts a reply inside an inline review thread."""
        ...


class Reviewer(ABC):
    """Produces a review for a PR given its diff."""

    @abstractmethod
    def review(self, event: PullRequestEvent, diff: str) -> Review:
        ...

    @abstractmethod
    def answer_query(self, event: ReviewCommentEvent, thread: list[dict], question: str) -> str:
        """Answers a user's question about one of the reviewer's inline comments."""
        ...
