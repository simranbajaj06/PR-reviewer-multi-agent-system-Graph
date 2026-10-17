"""GraphContext -> prompt text. Each agent gets the slice of the graph it needs.

Every line states the graph fact behind it ("calls it, 2 hops", "test reaches
it"), so a reviewer comment can cite its evidence.
"""
from __future__ import annotations

from src.pr_reviewer.graph.retriever import ChangedFunction, FunctionRef, GraphContext

ALL_SECTIONS = ("summary", "changed", "callers", "callees", "tests", "importers", "references")

ROLE_PRESETS: dict[str, tuple[str, ...]] = {
    "impact":    ("summary", "changed", "callers", "importers"),
    "tests":     ("summary", "changed", "tests", "callers"),
    "contracts": ("summary", "changed", "callees", "references", "callers"),
    "security":  ("summary", "changed", "callees", "references"),
    "general":   ALL_SECTIONS,
}

BASE_CODE_CHARS = 2_000
CALLER_CODE_CHARS = 800
CALLEE_CODE_CHARS = 600
CODE_FOR_NEAREST = 4          # callers whose code is shown in full; others get a signature


def _signature(fn: FunctionRef) -> str:
    for line in fn.code.splitlines():
        if line.strip() and not line.strip().startswith(("@", "#")):
            return line.strip()[:160]
    return fn.name


def _code(fn: FunctionRef, limit: int) -> str:
    code = fn.code if len(fn.code) <= limit else fn.code[:limit] + "\n... [truncated]"
    return f"```\n{code}\n```"


def _hops(n: int) -> str:
    return "calls it directly" if n == 1 else f"reaches it in {n} hops"


def _summary(ctx: GraphContext) -> str:
    lines = ["## Graph summary (base branch, from FalkorDB)"]
    for cf in ctx.changed:
        flags = []
        if cf.signature_touched:
            flags.append("SIGNATURE CHANGED")
        if not cf.tests:
            flags.append("no test reaches it")
        extra = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"- `{cf.fn.name}` ({cf.fn.loc}): risk={cf.risk}, {cf.fan_in} direct callers, "
            f"{cf.reach} production functions within {ctx.max_depth} hops, "
            f"{len(cf.tests)} tests{extra}"
        )
    if ctx.skipped_functions:
        lines.append(f"- (+{ctx.skipped_functions} more changed functions not expanded)")
    if ctx.new_files:
        lines.append(f"- New files (not in graph): {', '.join(ctx.new_files)}")
    if ctx.deleted_files:
        lines.append(f"- Deleted files: {', '.join(ctx.deleted_files)}")
    if ctx.module_level_files:
        lines.append(f"- Module-level changes (outside any function): {', '.join(ctx.module_level_files)}")
    return "\n".join(lines)


def _changed(ctx: GraphContext) -> str:
    parts = ["## Changed functions (code BEFORE this PR)"]
    for cf in ctx.changed:
        parts.append(f"### `{cf.fn.name}` - {cf.fn.loc}\n{_code(cf.fn, BASE_CODE_CHARS)}")
    return "\n\n".join(parts)


def _callers(ctx: GraphContext) -> str:
    parts = ["## Callers (who depends on the changed code)"]
    for cf in ctx.changed:
        if not cf.callers:
            parts.append(f"### `{cf.fn.name}`: no production callers found in the graph")
            continue
        lines = [f"### `{cf.fn.name}`: {cf.fan_in} direct, {cf.reach} total within {ctx.max_depth} hops"]
        for i, c in enumerate(cf.callers):
            lines.append(f"- {c.loc} `{c.name}` - {_hops(c.dist)}")
            if i < CODE_FOR_NEAREST and c.dist == 1:
                lines.append(_code(c, CALLER_CODE_CHARS))
            else:
                lines.append(f"  `{_signature(c)}`")
        if cf.reach > len(cf.callers):
            lines.append(f"- ... and {cf.reach - len(cf.callers)} more")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _callees(ctx: GraphContext) -> str:
    parts = ["## Dependencies (what the changed functions call)"]
    for cf in ctx.changed:
        if not cf.callees:
            continue
        lines = [f"### `{cf.fn.name}` calls:"]
        for c in cf.callees:
            lines.append(f"- {c.loc} `{c.name}`\n{_code(c, CALLEE_CODE_CHARS)}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if len(parts) > 1 else ""


def _tests(ctx: GraphContext) -> str:
    parts = ["## Test coverage (test functions that reach the changed code)"]
    for cf in ctx.changed:
        if cf.tests:
            lst = ", ".join(f"{t.loc} `{t.name}`" for t in cf.tests)
            parts.append(f"- `{cf.fn.name}`: {lst}")
        else:
            parts.append(f"- `{cf.fn.name}`: NO test in the graph reaches this function")
    return "\n".join(parts)


def _importers(ctx: GraphContext) -> str:
    if not ctx.importers:
        return ""
    lines = ["## Files importing the changed files"]
    for path, imps in ctx.importers.items():
        lines.append(f"- `{path}` is imported by: {', '.join(imps)}")
    return "\n".join(lines)


def _references(ctx: GraphContext) -> str:
    if not ctx.references:
        return ""
    lines = ["## Existing functions called by the NEW code (check arguments/return use against these)"]
    for name, defs in ctx.references.items():
        note = " (AMBIGUOUS name, several definitions)" if len(defs) > 1 else ""
        for d in defs:
            lines.append(f"- `{name}`{note} -> {d.loc}\n{_code(d, CALLEE_CODE_CHARS)}")
    return "\n".join(lines)


_RENDERERS = {
    "summary": _summary, "changed": _changed, "callers": _callers, "callees": _callees,
    "tests": _tests, "importers": _importers, "references": _references,
}


def render_context(
    ctx: GraphContext,
    sections: tuple[str, ...] | str = "general",
    max_chars: int = 12_000,
) -> str:
    """`sections` is a role name from ROLE_PRESETS or an explicit tuple."""
    if isinstance(sections, str):
        sections = ROLE_PRESETS[sections]
    if ctx.is_empty:
        return ""

    out: list[str] = []
    used = 0
    for name in sections:
        text = _RENDERERS[name](ctx)
        if not text:
            continue
        if used + len(text) > max_chars:
            room = max_chars - used
            if room > 300:
                out.append(text[:room] + "\n... [section truncated]")
            out.append(f"[context budget reached; skipped sections from '{name}' onward]")
            break
        out.append(text)
        used += len(text) + 2
    return "\n\n".join(out)
