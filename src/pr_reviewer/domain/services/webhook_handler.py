"""Core logic: verify -> dedupe -> dispatch by event type -> parse -> filter -> enqueue.
Depends only on the abstract ports (WebhookVerifier, ReviewQueue) - never on
FastAPI or a concrete GitHub adapter class. That's what makes it unit-testable
with fakes and swappable without changes here.
"""
from src.pr_reviewer.adapters.github.payload_parser import parse_pull_request_event
from src.pr_reviewer.adapters.github.push_parser import parse_push_event
from src.pr_reviewer.domain.interfaces import ReviewQueue, WebhookVerifier


class WebhookHandler:
    class InvalidSignature(Exception):
        pass

    def __init__(
        self,
        verifier: WebhookVerifier,
        review_queue: ReviewQueue,
        index_on_push: bool = False,
        index_branch: str | None = None,
    ):
        self._verifier = verifier
        self._queue = review_queue
        # When False (RAG disabled) push events are acknowledged but ignored.
        self._index_on_push = index_on_push
        # Override which branch is treated as the "default" for graph indexing.
        # Unset = use whatever GitHub reports as repository.default_branch.
        self._index_branch = index_branch
        # MVP idempotency guard against GitHub's at-least-once delivery retries.
        # In-memory + unbounded: fine for one process, but it resets on restart
        # and grows forever. Swap for a Redis SETNX with a TTL once you move
        # the queue off-process (same spot you'll swap InMemoryReviewQueue).
        self._seen_deliveries: set[str] = set()

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

        if delivery_id:
            if delivery_id in self._seen_deliveries:
                return {"status": "ignored", "reason": "duplicate delivery"}
            self._seen_deliveries.add(delivery_id)

        if event_type == "pull_request":
            result = self._handle_pull_request(payload)
        elif event_type == "push":
            result = self._handle_push(payload)
        else:
            result = {"status": "ignored", "reason": f"event type '{event_type}' not handled"}
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

    def _handle_push(self, payload: dict) -> dict:
        if not self._index_on_push:
            return {"status": "ignored", "reason": "indexing disabled"}

        job, reason = parse_push_event(payload, index_branch=self._index_branch)
        if job is None:
            return {"status": "ignored", "reason": reason}

        self._queue.enqueue(job)
        return {"status": "accepted", "branch": job.branch or "default", "index_sha": job.sha}