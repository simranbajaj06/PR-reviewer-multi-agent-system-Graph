"""Drains the queue and dispatches by job type:
  PullRequestEvent -> fetch diff -> make sure the code graph exists -> get a review -> post it back to the PR
  IndexJob         -> rebuild the code graph at the pushed commit
"""
import hashlib
import threading
import time
import traceback
from collections import OrderedDict

from src.pr_reviewer.adapters.github.diff_lines import commentable_lines
from src.pr_reviewer.adapters.persistence.db import save_review
from src.pr_reviewer.domain.interfaces import ReviewQueue, Reviewer, VCSClient
from src.pr_reviewer.domain.models import IndexJob, PullRequestEvent
from src.pr_reviewer.graph.updater import GraphUpdater


class _RecentReviews:
    """Remembers the last N (repo, PR, diff) combos that were reviewed AND posted.
    In-memory: cleared on restart, which is fine for webhook retries."""

    def __init__(self, max_items: int = 256):
        self._items: OrderedDict[str, None] = OrderedDict()
        self._max = max_items
        self._lock = threading.Lock()

    @staticmethod
    def key(repo: str, pr_number: int, diff: str) -> str:
        digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        return f"{repo}#{pr_number}:{digest}"

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._items

    def add(self, key: str) -> None:
        with self._lock:
            self._items[key] = None
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


_recent = _RecentReviews()


def _handle_review(
    event: PullRequestEvent,
    vcs_client: VCSClient,
    reviewer: Reviewer,
    updater: GraphUpdater | None,
) -> None:
    print(f"[worker] reviewing PR #{event.pr_number} on {event.repo_full_name}")

    try:
        diff = vcs_client.fetch_diff(event)
    except Exception as exc:
        print(f"[worker] failed to fetch diff for PR #{event.pr_number}: {exc}")
        return

    cache_key = _RecentReviews.key(event.repo_full_name, event.pr_number, diff)
    if _recent.seen(cache_key):
        print(f"[worker] PR #{event.pr_number}: diff unchanged since last review, skipping")
        return

    # First PR we ever see for this repo: build the graph so the reviewer has
    # context. Failure is non-fatal -- the reviewer should fall back to diff-only.
    if updater is not None:
        try:
            updater.ensure_indexed(event.repo_full_name)
        except Exception as exc:
            print(f"[worker] graph bootstrap failed for {event.repo_full_name}: {exc}")

    try:
        review = reviewer.review(event, diff)
    except Exception as exc:
        print(f"[worker] review failed for PR #{event.pr_number}: {exc}")
        return

    # Persist for the dashboard FIRST, so a review shows up even if posting to GitHub fails.
    try:
        review_id = save_review(
            repo=event.repo_full_name,
            pr_number=event.pr_number,
            pr_title=event.pr_title,
            head_sha=event.head_sha,
            verdict=review.verdict,
            summary=review.summary,
            overall_confidence=review.overall_confidence,
            needs_human_review=review.needs_human_review,
            findings=review.findings,
            graph_context=getattr(review, "graph_context", None),
        )
        print(f"[worker] saved review id={review_id} for dashboard ({len(review.findings)} findings)")
    except Exception:
        print("[worker] DB persistence FAILED, review will not show on the dashboard:")
        traceback.print_exc()

    # Build inline comment payloads from structured findings
    inline_findings = []
    for f in review.findings:
        # Only post inline comments for findings with file+line that have
        # reasonable confidence (skip the "agent failed" placeholder).
        if f.get("file") and f.get("line") and f.get("confidence", 0) > 0.3:
            inline_findings.append({
                "path": f["file"],
                "line": f["line"],
                "side": "RIGHT",
                "body": (
                    f"**[{f['severity'].upper()}] {f['category']}**\n"
                    f"{f['summary']}\n\n"
                    f"_{f.get('rationale', '')}_"
                ),
            })

    # GitHub rejects the WHOLE review (422) if any inline comment targets a line
    # that is not in the diff. Keep only valid ones; every finding is still in
    # the review body, so nothing is lost.
    valid = commentable_lines(diff)
    kept = [c for c in inline_findings if c["line"] in valid.get(c["path"], ())]
    if len(kept) != len(inline_findings):
        print(f"[worker] PR #{event.pr_number}: dropped {len(inline_findings) - len(kept)} "
              f"inline comment(s) pointing outside the diff")
    inline_findings = kept

    # Map internal verdict to GitHub review event
    event_map = {
        "approve": "APPROVE",
        "comment": "COMMENT",
        "request_changes": "REQUEST_CHANGES",
        "hold": "COMMENT",  # human must approve first
    }
    gh_event = event_map.get(review.verdict, "COMMENT")

    try:
        # Prefer the rich review API with inline comments; fall back to plain comment.
        if hasattr(vcs_client, "post_review") and inline_findings:
            vcs_client.post_review(
                event,
                review.summary,
                inline_findings,
                verdict=gh_event,
            )
        else:
            vcs_client.post_comment(event, review.summary)

        _recent.add(cache_key)
        print(f"[worker] posted review on PR #{event.pr_number}")
    except Exception as exc:
        print(f"[worker] failed to post review on PR #{event.pr_number}: {exc}")
        # Still try to post a plain comment as fallback
        try:
            vcs_client.post_comment(event, review.summary)
            _recent.add(cache_key)
        except Exception as exc2:
            print(f"[worker] fallback comment also failed: {exc2}")
            return


def _handle_index(job: IndexJob, updater: GraphUpdater | None) -> None:
    if updater is None:
        print(f"[worker] index job for {job.repo_full_name} skipped: graph disabled")
        return

    branch_label = job.branch or "default"
    print(f"[worker] syncing graph for {job.repo_full_name} branch={branch_label} @ {job.sha[:7]}")
    try:
        updater.sync(job.repo_full_name, job.sha, branch=job.branch)
        print(f"[worker] graph up to date for {job.repo_full_name} branch={branch_label} @ {job.sha[:7]}")
    except Exception as exc:
        print(f"[worker] graph sync failed for {job.repo_full_name} branch={branch_label}: {exc}")


def run_worker(
    review_queue: ReviewQueue,
    vcs_client: VCSClient,
    reviewer: Reviewer,
    updater: GraphUpdater | None = None,
    poll_interval: float = 1.0,
) -> None:
    print("[worker] started, waiting for jobs...")
    while True:
        job = review_queue.dequeue()
        if job is None:
            time.sleep(poll_interval)
            continue

        # A crash in one job must never kill this thread: if it dies, every later
        # webhook is accepted and queued but nothing is ever processed again.
        try:
            if isinstance(job, PullRequestEvent):
                print(f"[worker] picked up PR #{job.pr_number} ({job.action}) @ {job.head_sha[:7]}")
                _handle_review(job, vcs_client, reviewer, updater)
            elif isinstance(job, IndexJob):
                _handle_index(job, updater)
            else:
                print(f"[worker] ignoring unknown job type: {type(job).__name__}")
        except Exception:
            print("[worker] job crashed (worker keeps running):")
            traceback.print_exc()
