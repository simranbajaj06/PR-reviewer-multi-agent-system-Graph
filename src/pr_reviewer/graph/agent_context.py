"""Single place that decides what graph context each agent receives.

Used by the reviewer (at review time) and by the inspect CLI, so what you
inspect is exactly what the agents get.
"""
from __future__ import annotations

from src.pr_reviewer.domain.contracts import AgentType
from src.pr_reviewer.graph.render import render_context
from src.pr_reviewer.graph.retriever import GraphContext

# Which graph slices each agent sees (see ROLE_PRESETS in render.py).
AGENT_PRESET: dict[AgentType, str] = {
    AgentType.SECURITY: "security",   # callees + references (what new code calls)
    AgentType.QUALITY: "contracts",   # callees + references + callers
    AgentType.TESTS: "tests",         # tests + callers (coverage gaps)
    AgentType.DOCS: "impact",         # callers + importers (who is affected)
}
GENERAL_PRESET = "general"


def build_agent_contexts(ctx: GraphContext) -> tuple[str, dict[str, str]]:
    """-> (generic fallback text, {agent_type.value: text})"""
    generic = render_context(ctx, sections=GENERAL_PRESET, max_chars=12_000)
    agents: dict[str, str] = {}
    for agent_type, preset in AGENT_PRESET.items():
        text = render_context(ctx, sections=preset, max_chars=8_000)
        if text:
            agents[agent_type.value] = text
    return generic, agents


def describe(ctx: GraphContext) -> dict:
    """Compact, JSON-safe facts the graph found for this diff."""
    return {
        "changed": [
            {
                "name": cf.fn.name, "loc": cf.fn.loc, "risk": cf.risk,
                "signature_touched": cf.signature_touched,
                "fan_in": cf.fan_in, "reach": cf.reach,
                "callers": len(cf.callers), "callees": len(cf.callees), "tests": len(cf.tests),
            }
            for cf in ctx.changed
        ],
        "new_files": ctx.new_files,
        "deleted_files": ctx.deleted_files,
        "module_level_files": ctx.module_level_files,
        "importers": {k: len(v) for k, v in ctx.importers.items()},
        "references": sorted(ctx.references),
        "skipped_functions": ctx.skipped_functions,
    }
