"""Keeps the code graph in sync with a repo's branches.

  sync(repo, sha, branch)  -> bring the graph up to that commit
  ensure_indexed(repo)     -> build the graph if the repo has none yet (first PR we ever see)

How a sync works
  1. Same commit as the graph already reflects        -> nothing to do
  2. Download the commit as a tarball (one HTTP call, no git needed)
  3. Re-analyse the WHOLE repo with build_graph.analyze_repo (tree-sitter)
  4. Load it into a temporary graph and atomically swap it in (see GraphStore.replace)

Why a full rebuild instead of patching changed files: CALLS edges are resolved
by name across the whole repo ("the only function with that name"), so editing
one file can add or remove call edges from *other* files. Re-analysing
everything keeps the graph exactly equal to what `python build_graph.py` would
produce. Analysis is local CPU work (seconds for typical repos). If it ever
becomes too slow, an incremental path can reuse parse_source_file per file.

Run it from ONE thread per repo (the worker thread is fine): two concurrent
syncs would race on the temporary graph.
"""
from __future__ import annotations

import logging

from src.pr_reviewer.graph.build_graph import analyze_repo
from src.pr_reviewer.graph.ports import RepoSource
from src.pr_reviewer.graph.store import GraphStore

logger = logging.getLogger(__name__)


class GraphUpdater:
    def __init__(self, source: RepoSource, store: GraphStore):
        self._source = source
        self._store = store

    def sync(self, repo: str, ref: str | None = None, branch: str = "") -> str:
        """Rebuild the graph for `repo` at commit `ref` (or HEAD of default branch).

        `branch` is the short branch name (e.g. "feature/my-branch").
        Empty string  => default branch graph (key: owner__repo) — what PR reviews read.
        Non-empty str => isolated branch graph (key: owner__repo__branch) — never clobbers
                         the default-branch graph.
        """
        head = self._source.resolve_ref(repo, ref)

        if self._store.indexed_sha(repo, branch) == head and self._store.exists(repo, branch):
            logger.info("repo=%s branch=%r graph already at %s", repo, branch or "default", head[:7])
            return head

        logger.info("repo=%s branch=%r rebuilding graph @ %s", repo, branch or "default", head[:7])
        with self._source.snapshot(repo, head) as snap:
            files, functions, imports, calls = analyze_repo(snap.path)

        # An empty result almost always means a failed download/parse, not "the
        # repo has no code". Never replace a good graph with an empty one.
        if not functions:
            logger.warning("repo=%s branch=%r: no functions found at %s; graph left unchanged",
                           repo, branch or "default", head[:7])
            return head

        self._store.replace(repo, head, files, functions, imports, calls, branch=branch)
        return head

    def ensure_indexed(self, repo: str) -> None:
        """First-time bootstrap: a PR arrives for a repo that has no graph yet.
        Always targets the default branch (the graph PR reviews read from).
        """
        if not self._store.exists(repo):
            logger.info("repo=%s has no graph yet; building it from the default branch", repo)
            self.sync(repo)