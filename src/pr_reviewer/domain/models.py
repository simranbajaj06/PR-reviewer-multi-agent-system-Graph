"""Framework-agnostic data shapes. No FastAPI or GitHub-specific imports here."""
from dataclasses import dataclass, field

# The pull_request "action" values worth triggering a review for.
REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass(frozen=True)
class PullRequestEvent:
    """The subset of GitHub's payload our app actually needs."""
    action: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_sha: str
    head_ref: str
    base_ref: str
    author: str
    diff_url: str

    def is_reviewable(self) -> bool:
        return self.action in REVIEWABLE_ACTIONS


@dataclass(frozen=True)
class IndexJob:
    """Bring the RAG index for `repo_full_name` up to commit `sha`.

    `branch` is the short ref (e.g. "main", "feature/my-branch"). For the
    default branch the graph key is ``owner__repo``; for any other branch
    it is ``owner__repo__branch`` so it never overwrites the default-branch
    graph used by PR reviewers.
    """
    repo_full_name: str
    sha: str
    branch: str = ""           # empty = default branch (legacy compatibility)


@dataclass(frozen=True)
class ReviewCommentEvent:
    """The subset of a `pull_request_review_comment` payload we need."""
    action: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_sha: str
    comment_id: int
    in_reply_to_id: int | None   # id of the thread's first comment; None = new thread
    path: str
    line: int | None
    diff_hunk: str
    body: str
    author: str
    author_type: str             # "User" | "Bot" | ...


@dataclass(frozen=True)
class CommentQuery:
    """A user asked the bot a question inside an inline review thread.

    `question` is the comment text with the bot's own @mention removed.
    """
    event: ReviewCommentEvent
    question: str


# Anything that can travel through the queue.
QueueJob = PullRequestEvent | IndexJob | CommentQuery


@dataclass(frozen=True)
class Review:
    """The result of a reviewer pass over a PR's diff."""
    pr_number: int
    summary: str
    verdict: str  # "approve" | "comment" | "request_changes" | "hold"
    findings: list = field(default_factory=list)  # list[Finding] or raw dicts
    overall_confidence: float = 1.0
    needs_human_review: bool = False
    graph_context: dict = field(default_factory=dict)  # what the agents were given (for the dashboard)
