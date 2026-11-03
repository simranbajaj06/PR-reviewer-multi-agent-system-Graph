"""Core logic: verify -> dedupe -> dispatch by event type -> parse -> filter -> enqueue.
Depends only on the abstract ports (WebhookVerifier, ReviewQueue) - never on
FastAPI or a concrete GitHub adapter class. That's what makes it unit-testable
with fakes and swappable without changes here.
"""
from src.pr_reviewer.adapters.github.comment_parser import parse_review_comment_event
from src.pr_reviewer.adapters.github.payload_parser import parse_pull_request_event
from src.pr_reviewer.adapters.github.push_parser import parse_push_event
from src.pr_reviewer.domain.interfaces import DedupeStore, ReviewQueue, WebhookVerifier
from src.pr_reviewer.domain.mentions import decide, same_login
from src.pr_reviewer.domain.models import CommentQuery


class WebhookHandler:
    class InvalidSignature(Exception):
        pass

    _DELIVERY_TTL_S = 24 * 3600   # GitHub redelivers within hours, not days

    def __init__(
        self,
        verifier: WebhookVerifier,
        review_queue: ReviewQueue,
        index_on_push: bool = False,
        index_branch: str | None = None,
        bot_username: str = "pr-reviewer",
        dedupe: DedupeStore | None = None,
    ):
        self._verifier = verifier
        self._queue = review_queue
        # When False (RAG disabled) push events are acknowledged but ignored.
        self._index_on_push = index_on_push
        # Override which branch is treated as the "default" for graph indexing.
        # Unset = use whatever GitHub reports as repository.default_branch.
        self._index_branch = index_branch
        # Users address the bot with "@<bot_username>" in inline review threads.
        self._bot_username = bot_username
        # Idempotency guard against GitHub's at-least-once delivery retries.
        # Shared (Redis) so it works across processes/restarts and expires on its own.
        if dedupe is None:
            from src.pr_reviewer.adapters.dedupe.in_memory_dedupe import InMemoryDedupeStore
            dedupe = InMemoryDedupeStore()
        self._dedupe = dedupe

    def handle(
        self,
        payload_body: bytes,
        signature_header: str | None,
        payload: dict,
        delivery_id: str | None = None,
        event_type: str = "pull_request",
    ) -> dict:
        print(f"[webhook] event={event_type!r} action={payload.get('action')!r} delivery={delivery_id}")
        if not self._verifier.verify(payload_body, signature_header):
            raise WebhookHandler.InvalidSignature("signature verification failed")

        delivery_key = None
        if delivery_id:
            delivery_key = f"delivery:{delivery_id}"
            if not self._dedupe.claim(delivery_key, self._DELIVERY_TTL_S):
                return {"status": "ignored", "reason": "duplicate delivery"}

        try:
            if event_type == "pull_request":
                result = self._handle_pull_request(payload)
            elif event_type == "push":
                result = self._handle_push(payload)
            elif event_type == "pull_request_review_comment":
                result = self._handle_review_comment(payload)
            else:
                result = {"status": "ignored", "reason": f"event type '{event_type}' not handled"}
        except Exception:
            # e.g. the queue was unreachable: un-claim so GitHub's retry is not
            # mistaken for a duplicate and silently dropped.
            if delivery_key:
                self._dedupe.release(delivery_key)
            raise
        print(f"[webhook] -> {result}")
        return result

    def _handle_pull_request(self, payload: dict) -> dict:
        if "pull_request" not in payload or "action" not in payload:
            return {"status": "ignored", "reason": "not a pull_request event"}

        event = parse_pull_request_event(payload)

        if not event.is_reviewable():
            return {"status": "ignored", "reason": f"action '{event.action}' not reviewable"}

        self._queue.enqueue(event)
        return {"status": "accepted", "pr_number": event.pr_number}

    def _handle_review_comment(self, payload: dict) -> dict:
        """A comment on an inline review thread. Only answered when it is a reply
        that @mentions the bot; comments aimed at other people are left alone."""
        if payload.get("action") != "created":
            return {"status": "ignored", "reason": f"action '{payload.get('action')}' not handled"}
        if "comment" not in payload or "pull_request" not in payload:
            return {"status": "ignored", "reason": "not a review comment event"}

        event = parse_review_comment_event(payload)

        # Never react to bots (including ourselves) -- prevents reply loops.
        if event.author_type == "Bot" or same_login(event.author, self._bot_username):
            return {"status": "ignored", "reason": "comment written by a bot"}
        if event.in_reply_to_id is None:
            return {"status": "ignored", "reason": "not a reply inside an existing thread"}

        decision = decide(event.body, self._bot_username)
        if not decision.should_respond:
            return {"status": "ignored", "reason": decision.reason}

        self._queue.enqueue(CommentQuery(event=event, question=decision.question))
        return {"status": "accepted", "comment_id": event.comment_id}

    def _handle_push(self, payload: dict) -> dict:
        if not self._index_on_push:
            return {"status": "ignored", "reason": "indexing disabled"}

        job, reason = parse_push_event(payload, index_branch=self._index_branch)
        if job is None:
            return {"status": "ignored", "reason": reason}

        self._queue.enqueue(job)
        return {"status": "accepted", "branch": job.branch or "default", "index_sha": job.sha}