"""FalkorDB access for the app: one graph per repository (and per branch).

  graph_name("owner/name")               -> "owner__name"              (default branch)
  graph_name("owner/name", "feat/foo")   -> "owner__name__feat_foo"    (feature branch)
  (or GRAPH_NAME for every repo, see below)

Responsibilities
  * exists / indexed_sha   -> is there a graph, and which commit does it reflect?
  * replace(...)           -> load a freshly analysed repo and swap it in ATOMICALLY
  * context(repo, diff)    -> the reviewer's read path (Cypher via GraphContextRetriever)

Atomic swap: the new graph is built under "<name>__building" and then moved onto
"<name>" with a Redis RENAME (graphs are Redis keys). Reviews running during a
rebuild keep reading the old graph; they never see a half-built one.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from src.pr_reviewer.graph.build_graph import load_graph
from src.pr_reviewer.graph.retriever import GraphContext, GraphContextRetriever

logger = logging.getLogger(__name__)

_BUILDING_SUFFIX = "__building"


class GraphStore:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        password: str | None = None,
        *,
        fixed_graph_name: str | None = None,
        db: Any = None,                     # inject a FalkorDB-compatible client (tests)
        retriever_opts: dict | None = None,
    ):
        if db is None:
            from falkordb import FalkorDB
            db = FalkorDB(host=host, port=port, password=password or None)
        self._db = db
        self._fixed = fixed_graph_name
        self._retriever_opts = retriever_opts or {}
        self._indexed: set[str] = set()     # graphs we already ran ensure_indexes() on

    # ---- naming ---------------------------------------------------------------
    def graph_name(self, repo: str, branch: str = "") -> str:
        """Return the FalkorDB graph key for this repo (and optional branch).

        * GRAPH_NAME env var overrides everything (manual / single-graph setups).
        * Default branch  -> ``owner__repo``         (what PR reviews read)
        * Feature branch  -> ``owner__repo__branch`` (isolated; never clobbers default)
        """
        if self._fixed:
            return self._fixed
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", repo.replace("/", "__"))
        if branch:
            safe_branch = re.sub(r"[^A-Za-z0-9_.-]", "_", branch)
            return f"{base}__{safe_branch}"
        return base

    def _graph(self, repo: str, branch: str = ""):
        return self._db.select_graph(self.graph_name(repo, branch))

    # ---- health / state ---------------------------------------------------------
    def ping(self) -> None:
        self._db.connection.ping()

    def close(self) -> None:
        try:
            self._db.connection.close()
        except Exception:
            pass

    def exists(self, repo: str, branch: str = "") -> bool:
        """True if the repo's graph (for the given branch) has at least one File node."""
        try:
            res = self._graph(repo, branch).ro_query("MATCH (f:File) RETURN count(f) AS n")
            return bool(res.result_set and res.result_set[0][0] > 0)
        except Exception:
            return False        # querying a missing graph raises "empty key"

    def indexed_sha(self, repo: str, branch: str = "") -> str | None:
        """Commit the graph reflects (stored on a Meta node), None if unknown.
        A graph built by hand with build_graph.py has no Meta node -> None."""
        try:
            res = self._graph(repo, branch).ro_query("MATCH (m:Meta) RETURN m.sha LIMIT 1")
            return res.result_set[0][0] if res.result_set else None
        except Exception:
            return None

    # ---- explorer (read-only, used by the dashboard) --------------------------
    @staticmethod
    def _rows(res) -> list[dict]:
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        return [dict(zip(cols, row)) for row in res.result_set]

    def list_repos(self) -> list[str]:
        names = [n for n in self._db.list_graphs() if not n.endswith(_BUILDING_SUFFIX)]
        if self._fixed:
            return [self._fixed]
        # Return only default-branch graphs (no __ after the repo separator)
        # so the dashboard doesn't list every feature branch separately.
        return sorted(n.replace("__", "/", 1) for n in names
                      if n.count("__") == 1)  # exactly one __ = owner__repo

    def overview(self, repo: str) -> dict:
        g = self._graph(repo)
        one = lambda q: g.ro_query(q).result_set[0][0]  # noqa: E731
        meta = g.ro_query("MATCH (m:Meta) RETURN m.sha, m.indexed_at LIMIT 1").result_set
        return {
            "sha": meta[0][0] if meta else None,
            "indexed_at": meta[0][1] if meta else None,
            "files": one("MATCH (f:File) RETURN count(f)"),
            "test_files": one("MATCH (f:File) WHERE f.is_test RETURN count(f)"),
            "functions": one("MATCH (f:Function) RETURN count(f)"),
            "calls": one("MATCH ()-[r:CALLS]->() RETURN count(r)"),
            "imports": one("MATCH ()-[r:IMPORTS]->() RETURN count(r)"),
            "languages": self._rows(g.ro_query(
                "MATCH (f:File) RETURN f.language AS language, count(f) AS n ORDER BY n DESC")),
            "hotspots": self._rows(g.ro_query(
                "MATCH (c:Function)-[:CALLS]->(f:Function) "
                "RETURN f.id AS id, f.name AS name, f.file AS file, count(DISTINCT c) AS callers "
                "ORDER BY callers DESC LIMIT 10")),
        }

    def search_functions(self, repo: str, q: str, limit: int = 25) -> list[dict]:
        return self._rows(self._graph(repo).ro_query(
            "MATCH (fn:Function) WHERE toLower(fn.name) CONTAINS toLower($q) "
            "RETURN fn.id AS id, fn.name AS name, fn.file AS file, fn.start_line AS start_line "
            "ORDER BY fn.name LIMIT $limit", {"q": q, "limit": limit}))

    def function_view(self, repo: str, fn_id: str, depth: int = 2, limit: int = 15) -> dict | None:
        from src.pr_reviewer.graph import queries as Q
        g = self._graph(repo)
        p = {"id": fn_id, "limit": limit}
        head = self._rows(g.ro_query(
            "MATCH (f:Function {id: $id}) RETURN f.id AS id, f.name AS name, f.file AS file, "
            "f.start_line AS start_line, f.end_line AS end_line, f.code AS code", p))
        if not head:
            return None
        return {
            "function": head[0],
            "callers": self._rows(g.ro_query(Q.callers(depth), p)),
            "callees": self._rows(g.ro_query(Q.CALLEES, p)),
            "tests": self._rows(g.ro_query(Q.tests(depth), p)),
        }

    # ---- write path -------------------------------------------------------------
    def replace(self, repo: str, sha: str, files, functions, imports, calls,
                branch: str = "") -> None:
        name = self.graph_name(repo, branch)
        tmp_name = name + _BUILDING_SUFFIX

        try:                                   # clear leftovers of a crashed build
            self._db.select_graph(tmp_name).delete()
        except Exception:
            pass

        tmp = self._db.select_graph(tmp_name)
        try:
            load_graph(tmp, files, functions, imports, calls)
            tmp.query("CREATE (:Meta {sha: $sha, indexed_at: timestamp()})", {"sha": sha})
            self._db.connection.rename(tmp_name, name)   # atomic; overwrites the live graph
        except Exception:
            try:
                tmp.delete()                   # never leave a half-built graph behind
            except Exception:
                pass
            raise
        logger.info("graph '%s' now at %s (%d files, %d functions, %d calls)",
                    name, sha[:7], len(files), len(functions), len(calls))

    # ---- read path --------------------------------------------------------------
    def context(self, repo: str, diff: str) -> GraphContext:
        name = self.graph_name(repo)
        retriever = GraphContextRetriever(self._db.select_graph(name), **self._retriever_opts)
        if name not in self._indexed:       # covers graphs built by an older build_graph.py
            retriever.ensure_indexes()
            self._indexed.add(name)
        return retriever.retrieve(diff)