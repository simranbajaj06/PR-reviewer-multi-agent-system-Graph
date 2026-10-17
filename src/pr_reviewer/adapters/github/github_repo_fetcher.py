"""Downloads a snapshot of a GitHub repo (no git needed) into a temp dir so the
graph builder can analyse it. Uses the tarball API: one HTTP request instead of
a clone.

  with fetch_snapshot("owner/name") as snap:        # default branch
      analyze_repo(snap.path)

Private repos need GITHUB_TOKEN with read access to repository contents.
"""
import os
import re
import shutil
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

from src.pr_reviewer.graph.ports import RepoSource, Snapshot

API = "https://api.github.com"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _headers(token: str | None, accept: str) -> dict:
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract regular files/dirs only; refuse anything escaping `dest`."""
    dest = dest.resolve()
    for member in tar:
        if not (member.isfile() or member.isdir()):
            continue  # skip symlinks, devices, etc.
        target = (dest / member.name).resolve()
        if dest != target and dest not in target.parents:
            raise ValueError(f"unsafe path in tarball: {member.name}")
        tar.extract(member, dest)


def _resolve_ref(repo: str, ref: str | None, token: str | None) -> str:
    """Branch/tag/sha (None = default branch) -> exact commit sha."""
    if ref and _SHA_RE.match(ref):
        return ref
    if ref is None:
        r = requests.get(
            f"{API}/repos/{repo}",
            headers=_headers(token, "application/vnd.github+json"),
            timeout=15,
        )
        r.raise_for_status()
        ref = r.json()["default_branch"]
    r = requests.get(
        f"{API}/repos/{repo}/commits/{ref}",
        headers=_headers(token, "application/vnd.github.sha"),
        timeout=15,
    )
    r.raise_for_status()
    return r.text.strip()


@contextmanager
def fetch_snapshot(
    repo: str, ref: str | None = None, token: str | None = None
) -> Iterator[Snapshot]:
    """Yields a Snapshot of `repo` at `ref` (default: the repo's default
    branch). The temp dir is deleted when the block exits."""
    token = token or os.environ.get("GITHUB_TOKEN")
    commit_sha = _resolve_ref(repo, ref, token)

    tmp = Path(tempfile.mkdtemp(prefix="repo-snap-"))
    try:
        archive = tmp / "repo.tar.gz"
        with requests.get(
            f"{API}/repos/{repo}/tarball/{commit_sha}",
            headers=_headers(token, "application/vnd.github+json"),
            stream=True,
            timeout=60,
        ) as resp:
            resp.raise_for_status()
            with open(archive, "wb") as f:
                for block in resp.iter_content(1 << 20):
                    f.write(block)

        extract_dir = tmp / "src"
        extract_dir.mkdir()
        with tarfile.open(archive) as tar:
            _safe_extract(tar, extract_dir)

        # GitHub wraps everything in one top-level "owner-repo-<sha>/" folder.
        (root,) = [p for p in extract_dir.iterdir() if p.is_dir()]
        yield Snapshot(repo=repo, ref=ref or commit_sha, commit_sha=commit_sha, path=root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class GitHubSource(RepoSource):
    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("GITHUB_TOKEN")

    def resolve_ref(self, repo: str, ref: str | None) -> str:
        return _resolve_ref(repo, ref, self._token)

    def snapshot(self, repo: str, sha: str):
        return fetch_snapshot(repo, sha, self._token)