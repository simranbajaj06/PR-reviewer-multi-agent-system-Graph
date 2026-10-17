"""Turns a PR diff into structured context from the FalkorDB code graph.

    diff --parse--> exact old-side changed lines
         --Q1-->    changed Function nodes
         --Q2..Q6--> per function: fan-in, blast radius, callers, callees, tests
         --Q7-->    per file: importers
         --Q8-->    existing definitions referenced by newly added code

No embeddings, no similarity thresholds: every item in the result is there
because of an edge in the graph, so every item can be explained.

The graph reflects the base branch (it is built before the PR), so line
numbers on Function nodes are old-side numbers. Brand-new functions in the PR
are not in the graph; what they *use* is found by Q8.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from src.pr_reviewer.graph import queries as Q
from src.pr_reviewer.graph.diff_parser import FileDiff, parse_diff

logger = logging.getLogger(__name__)

_CALL_NAME = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


@dataclass(frozen=True)
class FunctionRef:
    id: str
    name: str
    file: str
    start_line: int
    end_line: int
    code: str = ""
    dist: int = 0            # hops from the changed function (0 = not applicable)

    @classmethod
    def from_row(cls, row: dict, dist: int = 0) -> "FunctionRef":
        return cls(
            id=row["id"], name=row["name"], file=row["file"],
            start_line=int(row["start_line"]), end_line=int(row["end_line"]),
            code=row.get("code") or "", dist=dist,
        )

    @property
    def loc(self) -> str:
        return f"{self.file}:{self.start_line}-{self.end_line}"


@dataclass
class ChangedFunction:
    fn: FunctionRef
    signature_touched: bool           # the line declaring it was removed/rewritten
    fan_in: int                       # direct callers
    reach: int                        # production functions within max_depth hops
    callers: list[FunctionRef] = field(default_factory=list)
    callees: list[FunctionRef] = field(default_factory=list)
    tests: list[FunctionRef] = field(default_factory=list)

    @property
    def risk(self) -> str:
        if self.signature_touched and self.fan_in:
            return "high"
        if self.reach >= 10 or self.fan_in >= 5:
            return "high"
        if self.reach >= 3 or not self.tests:
            return "medium"
        return "low"


@dataclass
class GraphContext:
    changed: list[ChangedFunction] = field(default_factory=list)
    importers: dict[str, list[str]] = field(default_factory=dict)       # changed file -> importing files
    references: dict[str, list[FunctionRef]] = field(default_factory=dict)  # symbol used by new code -> definitions
    new_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    module_level_files: list[str] = field(default_factory=list)  # changed, but not inside any function
    skipped_functions: int = 0        # changed functions dropped by max_changed
    max_depth: int = 3

    @property
    def is_empty(self) -> bool:
        return not (self.changed or self.references or self.importers
                    or self.new_files or self.deleted_files)


class GraphContextRetriever:
    def __init__(
        self,
        graph: Any,                    # falkordb.Graph (anything with ro_query/query)
        *,
        max_depth: int = 3,
        max_changed: int = 15,
        callers_limit: int = 15,
        callees_limit: int = 10,
        tests_limit: int = 10,
        importers_limit: int = 15,
        max_symbols: int = 40,
    ):
        self._graph = graph
        self._depth = max_depth
        self._max_changed = max_changed
        self._callers_limit = callers_limit
        self._callees_limit = callees_limit
        self._tests_limit = tests_limit
        self._importers_limit = importers_limit
        self._max_symbols = max_symbols

    # ---- construction helpers -------------------------------------------------
    @classmethod
    def from_env(cls, graph_name: str = "code_graph", **kw) -> "GraphContextRetriever":
        import os
        from dotenv import load_dotenv
        load_dotenv()
        from falkordb import FalkorDB

        db = FalkorDB(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            password=os.getenv("FALKORDB_PASSWORD") or None,
        )
        return cls(db.select_graph(graph_name), **kw)

    def ensure_indexes(self) -> None:
        for q in Q.CREATE_INDEXES:
            try:
                self._graph.query(q)
            except Exception:
                pass  # already exists

    # ---- query plumbing -------------------------------------------------------
    def _run(self, query: str, params: dict | None = None) -> list[dict]:
        run = getattr(self._graph, "ro_query", None) or self._graph.query
        res = run(query, params or {})
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        return [dict(zip(cols, row)) for row in res.result_set]

    def _count(self, query: str, params: dict) -> int:
        rows = self._run(query, params)
        return int(rows[0]["n"]) if rows else 0

    # ---- main entry point -----------------------------------------------------
    def retrieve(self, diff: str) -> GraphContext:
        t0 = perf_counter()
        files = parse_diff(diff)
        ctx = GraphContext(max_depth=self._depth)
        if not files:
            return ctx

        ctx.new_files = [f.path for f in files if f.is_new]
        ctx.deleted_files = [f.path for f in files if f.is_deleted]
        by_graph_path = {f.graph_path: f for f in files if f.graph_path}

        # Q1: changed functions
        rows = [
            {"file": f.graph_path, "lo": lo, "hi": hi}
            for f in files if f.graph_path for lo, hi in f.ranges
        ]
        changed_refs: list[FunctionRef] = []
        if rows:
            changed_refs = sorted(
                (FunctionRef.from_row(r) for r in self._run(Q.CHANGED_FUNCTIONS, {"ranges": rows})),
                key=lambda r: (r.file, r.start_line),
            )
        ctx.skipped_functions = max(0, len(changed_refs) - self._max_changed)
        changed_refs = changed_refs[: self._max_changed]

        hit_files = {r.file for r in changed_refs}
        ctx.module_level_files = [
            f.graph_path for f in files
            if f.graph_path and f.ranges and f.graph_path not in hit_files and not f.is_deleted
        ]

        # Q2-Q6: expand each changed function
        for ref in changed_refs:
            ctx.changed.append(self._expand(ref, by_graph_path.get(ref.file)))

        # Q7: who imports the changed files?
        for path in list(dict.fromkeys(hit_files | set(ctx.module_level_files)))[: self._importers_limit]:
            imps = self._run(Q.IMPORTERS, {"path": path, "limit": self._importers_limit})
            if imps:
                ctx.importers[path] = [r["path"] for r in imps]

        # Q8: existing code that the NEW code calls
        ctx.references = self._resolve_references(files, {r.id for r in changed_refs})

        logger.info(
            "graph retrieval: files=%d changed_fns=%d refs=%d (%.2fs)",
            len(files), len(ctx.changed), len(ctx.references), perf_counter() - t0,
        )
        return ctx

    # ---- pieces ---------------------------------------------------------------
    def _expand(self, ref: FunctionRef, fd: FileDiff | None) -> ChangedFunction:
        p = {"id": ref.id}
        return ChangedFunction(
            fn=ref,
            signature_touched=_signature_touched(ref, fd),
            fan_in=self._count(Q.FAN_IN, p),
            reach=self._count(Q.reach_count(self._depth), p),
            callers=[
                FunctionRef.from_row(r, dist=int(r["dist"]))
                for r in self._run(Q.callers(self._depth), {**p, "limit": self._callers_limit})
            ],
            callees=[
                FunctionRef.from_row(r)
                for r in self._run(Q.CALLEES, {**p, "limit": self._callees_limit})
            ],
            tests=[
                FunctionRef.from_row(r)
                for r in self._run(Q.tests(self._depth), {**p, "limit": self._tests_limit})
            ],
        )

    def _resolve_references(self, files: list[FileDiff], changed_ids: set[str]) -> dict[str, list[FunctionRef]]:
        names: list[str] = []
        for f in files:
            for line in f.added:
                names.extend(_CALL_NAME.findall(line))
        names = list(dict.fromkeys(names))[: self._max_symbols]
        if not names:
            return {}

        out: dict[str, list[FunctionRef]] = {}
        for r in self._run(Q.SYMBOL_LOOKUP, {"names": names, "limit": self._max_symbols * 3}):
            ref = FunctionRef.from_row(r)
            if ref.id in changed_ids:
                continue
            bucket = out.setdefault(r["ref"], [])
            if len(bucket) < 3:          # >1 candidate = ambiguous name; the agent is told
                bucket.append(ref)
        return out


def _signature_touched(ref: FunctionRef, fd: FileDiff | None) -> bool:
    """True if the line that declares the function was removed or rewritten."""
    if fd is None or not fd.removed:
        return False
    pat = re.compile(rf"\b{re.escape(ref.name)}\s*\(")
    sig = next((l.strip() for l in ref.code.splitlines() if pat.search(l)), None)
    return sig is not None and sig in {l.strip() for l in fd.removed}
