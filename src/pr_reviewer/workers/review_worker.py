"""Drains the queue and dispatches by job type:
  PullRequestEvent -> fetch diff -> make sure the code graph exists -> get a review -> post it back to the PR
  IndexJob         -> rebuild the code graph at the pushed commit
  CommentQuery     -> a user @mentioned the bot in one of its inline threads -> answer in the thread
"""
import hashlib
import threading
import time
import traceback

from src.pr_reviewer.adapters.github.diff_lines import commentable_lines
from src.pr_reviewer.adapters.persistence.db import save_review
from src.pr_reviewer.domain.interfaces import DedupeStore, ReviewQueue, Reviewer, VCSClient
from src.pr_reviewer.domain.mentions import same_login
from src.pr_reviewer.domain.models import CommentQuery, IndexJob, PullRequestEvent
from src.pr_reviewer.graph.updater import GraphUpdater


_REVIEWED_TTL_S = 7 * 24 * 3600


def _review_key(repo: str, pr_number: int, diff: str) -> str:
    """Identifies one (repo, PR, exact diff) combination."""
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return f"reviewed:{repo}#{pr_number}:{digest}"


def _handle_review(
    event: PullRequestEvent,
    vcs_client: VCSClient,
    reviewer: Reviewer,
    updater: GraphUpdater | None,
    dedupe: DedupeStore,
) -> None:
    print(f"[worker] reviewing PR #{event.pr_number} on {event.repo_full_name}")

    try:
        diff = vcs_client.fetch_diff(event)
    except Exception as exc:
        print(f"[worker] failed to fetch diff for PR #{event.pr_number}: {exc}")
        raise   # transient (GitHub 5xx, network): let the queue retry this job

    cache_key = _review_key(event.repo_full_name, event.pr_number, diff)
    if dedupe.seen(cache_key):
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
        raise   # e.g. LLM rate limit / timeout: retry via the queue

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

        dedupe.mark(cache_key, _REVIEWED_TTL_S)
        print(f"[worker] posted review on PR #{event.pr_number}")
    except Exception as exc:
        print(f"[worker] failed to post review on PR #{event.pr_number}: {exc}")
        # Still try to post a plain comment as fallback
        try:
            vcs_client.post_comment(event, review.summary)
            dedupe.mark(cache_key, _REVIEWED_TTL_S)
        except Exception as exc2:
            print(f"[worker] fallback comment also failed: {exc2}")
            raise   # nothing was posted: retry the whole job later


def _handle_comment_query(
    job: CommentQuery,
    vcs_client: VCSClient,
    reviewer: Reviewer,
    bot_username: str,
) -> None:
    """Answers a question a user asked (with an @mention) under one of OUR inline comments."""
    ev = job.event
    root_id = ev.in_reply_to_id
    tag = f"PR #{ev.pr_number} comment {ev.comment_id}"

    try:
        thread = vcs_client.fetch_review_thread(ev.repo_full_name, ev.pr_number, root_id)
    except Exception as exc:
        print(f"[worker] {tag}: could not fetch thread: {exc}")
        raise

    # Only answer in threads the reviewer started -- not in human<->human threads.
    if not thread or not same_login(thread[0]["author"], bot_username):
        print(f"[worker] {tag}: thread was not started by @{bot_username}, ignoring")
        return

    # Already answered (webhook redelivery / restart)? Don't answer twice.
    if any(c["id"] > ev.comment_id and same_login(c["author"], bot_username) for c in thread):
        print(f"[worker] {tag}: already answered, skipping")
        return

    try:
        answer = reviewer.answer_query(ev, thread, job.question)
    except Exception as exc:
        print(f"[worker] {tag}: failed to generate answer: {exc}")
        answer = "Sorry, I couldn't process that question right now. Please try again in a moment."

    if not answer:
        print(f"[worker] {tag}: empty answer, not replying")
        return

    try:
        vcs_client.reply_to_review_comment(ev.repo_full_name, ev.pr_number, root_id, answer)
        print(f"[worker] {tag}: replied to @{ev.author}")
    except Exception as exc:
        print(f"[worker] {tag}: failed to post reply: {exc}")
        raise


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
        raise


def run_worker(
    review_queue: ReviewQueue,
    vcs_client: VCSClient,
    reviewer: Reviewer,
    updater: GraphUpdater | None = None,
    poll_interval: float = 1.0,
    bot_username: str = "pr-reviewer",
    dedupe: DedupeStore | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Main loop. Runs until `stop_event` is set (graceful shutdown) or forever.

    poll_interval is only used when dequeue() returns instantly with nothing
    (the in-memory queue). The Redis queue long-polls, so it can be 0."""
    if dedupe is None:
        from src.pr_reviewer.adapters.dedupe.in_memory_dedupe import InMemoryDedupeStore
        dedupe = InMemoryDedupeStore()
    stop = stop_event or threading.Event()

    print("[worker] started, waiting for jobs...")
    while not stop.is_set():
        # The queue backend (Redis) can be briefly unreachable. That must not kill the loop.
        try:
            job = review_queue.dequeue()
        except Exception as exc:
            print(f"[worker] queue unavailable ({exc}); retrying in 3s")
            stop.wait(3)
            continue

        if job is None:
            if poll_interval:
                stop.wait(poll_interval)
            continue

        # A crash in one job must never kill this loop: if it dies, every later
        # webhook is accepted and queued but nothing is ever processed again.
        try:
            if isinstance(job, PullRequestEvent):
                print(f"[worker] picked up PR #{job.pr_number} ({job.action}) @ {job.head_sha[:7]}")
                _handle_review(job, vcs_client, reviewer, updater, dedupe)
            elif isinstance(job, IndexJob):
                _handle_index(job, updater)
            elif isinstance(job, CommentQuery):
                _handle_comment_query(job, vcs_client, reviewer, bot_username)
            else:
                print(f"[worker] ignoring unknown job type: {type(job).__name__}")
            review_queue.ack(job)
        except Exception:
            print("[worker] job failed (worker keeps running; queue will retry or dead-letter it):")
            traceback.print_exc()
            try:
                review_queue.fail(job)
            except Exception:
                pass
    print("[worker] stopped")
