"""Concrete VCSClient for GitHub - fetches the raw diff and posts comments back."""
import os
import requests

from src.pr_reviewer.domain.interfaces import VCSClient
from src.pr_reviewer.domain.models import PullRequestEvent


class GitHubClient(VCSClient):
    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("GITHUB_TOKEN")
        if not self._token:
            raise ValueError(
                "GITHUB_TOKEN is not set. GitHubClient needs it to post "
                "comments (and to reliably fetch diffs without hitting the "
                "anonymous rate limit). Set it in your environment or .env file."
            )
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def fetch_diff(self, event: PullRequestEvent) -> str:
        # Uses api.github.com directly, NOT event.diff_url - that field
        # redirects to a different host, and requests strips the Authorization
        # header on cross-domain redirects, silently breaking auth for private repos.
        url = f"https://api.github.com/repos/{event.repo_full_name}/pulls/{event.pr_number}"
        headers = {"Accept": "application/vnd.github.v3.diff"}
        resp = self._session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.text

    def post_comment(self, event: PullRequestEvent, body: str) -> None:
        """Posts a general (non-inline) comment on the pull request."""
        url = (
            f"https://api.github.com/repos/{event.repo_full_name}"
            f"/issues/{event.pr_number}/comments"
        )
        resp = self._session.post(url, json={"body": body}, timeout=15)
        resp.raise_for_status()

    def post_review(
        self,
        event: PullRequestEvent,
        body: str,
        findings: list[dict],
        verdict: str = "COMMENT",
    ) -> None:
        """Posts a proper pull-request review with inline line-level comments.

        `findings` must be dicts with keys: path, line, side (RIGHT/LEFT), body.
        `verdict` is one of APPROVE, COMMENT, REQUEST_CHANGES.
        """
        # GitHub caps review comments at a reasonable number; batch the rest
        # into the top-level body if there are too many.
        MAX_INLINE = 20
        inline = findings[:MAX_INLINE]
        overflow = findings[MAX_INLINE:]

        if overflow:
            body += f"\n\n> _{len(overflow)} more findings omitted from inline view._"

        payload: dict = {
            "body": body,
            "event": verdict,
            "comments": [
                {
                    "path": f["path"],
                    "line": f["line"],
                    "side": f.get("side", "RIGHT"),
                    "body": f["body"],
                }
                for f in inline
                if f.get("path") and f.get("line")
            ],
        }

        url = (
            f"https://api.github.com/repos/{event.repo_full_name}"
            f"/pulls/{event.pr_number}/reviews"
        )
        resp = self._session.post(url, json=payload, timeout=30)
        if not resp.ok:
            raise requests.HTTPError(f"{resp.status_code} {resp.reason}: {resp.text[:500]}", response=resp)
