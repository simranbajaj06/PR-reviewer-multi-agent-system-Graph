"""Concrete Reviewer adapter: builds the Groq LLM client and the
multi-agent orchestrator graph, and translates between the domain's
PullRequestEvent/Review and the graph's internal ReviewState.

If a GraphStore is supplied, relevant existing code is retrieved once
per review (not once per specialist), rendered into per-agent prompt
slices, and handed to the graph as `context` + `agent_context`.
"""
import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain_groq import ChatGroq

from src.pr_reviewer.domain.contracts import AgentType
from src.pr_reviewer.domain.interfaces import Reviewer
from src.pr_reviewer.domain.models import PullRequestEvent, Review
from src.pr_reviewer.graph.agent_context import build_agent_contexts, describe
from src.pr_reviewer.orchestrator.graph import build_graph

load_dotenv()
logger = logging.getLogger(__name__)


class GroqReviewer(Reviewer):
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        graph_store: Any | None = None,  # GraphStore | None (avoid circular import)
    ):
        # Default: llama-3.3-70b-versatile (fast, good for structured output).
        # For deeper reasoning, openai/gpt-oss-120b is also on Groq's free tier.
        self._model_name = model or os.getenv("GROK_MODEL", "llama-3.3-70b-versatile")
        llm = ChatGroq(
            model=self._model_name,
            api_key=api_key or os.getenv("GROQ_API_KEY"),
            temperature=0,
            timeout=90,
            max_retries=2,
        )
        self._graph = build_graph(llm)
        self._graph_store = graph_store

    def _build_context(self, event: PullRequestEvent, diff: str) -> tuple[str, dict[str, str], dict]:
        """Returns (generic_context, {agent: context}, meta). `meta` records what the
        agents were given, or why they got nothing, so the dashboard can show it."""
        if self._graph_store is None:
            return "", {}, {"status": "disabled", "detail": "graph store not configured"}
        try:
            ctx = self._graph_store.context(event.repo_full_name, diff)
        except Exception as exc:
            logger.warning("PR #%d graph context failed (%s); falling back to diff-only review",
                           event.pr_number, exc)
            return "", {}, {"status": "error", "detail": str(exc)}

        if ctx.is_empty:
            return "", {}, {"status": "empty", "detail": "graph found nothing related to this diff",
                            **describe(ctx)}

        generic, agent_ctx = build_agent_contexts(ctx)
        logger.info("PR #%d context built: generic=%d chars, agents=%s", event.pr_number,
                    len(generic), {k: f"{len(v)}ch" for k, v in agent_ctx.items()})
        return generic, agent_ctx, {"status": "ok", **describe(ctx), "generic": generic, "agents": agent_ctx}

    def review(self, event: PullRequestEvent, diff: str) -> Review:
        generic_context, agent_context, ctx_meta = self._build_context(event, diff)

        result = self._graph.invoke({
            "pr_number": event.pr_number,
            "pr_title": event.pr_title,
            "diff": diff,
            "context": generic_context,
            "agent_context": agent_context,
            "findings": [],
            "overall_confidence": 1.0,
            "needs_human_review": False,
            "summary": "",
        })

        verdict = "hold" if result["needs_human_review"] else "comment"

        # Convert Pydantic Finding objects to plain dicts for serialization
        findings = []
        for f in result.get("findings", []):
            if hasattr(f, "model_dump"):
                findings.append(f.model_dump())
            elif hasattr(f, "dict"):
                findings.append(f.dict())
            else:
                findings.append(f)

        return Review(
            pr_number=event.pr_number,
            summary=result["summary"],
            verdict=verdict,
            findings=findings,
            overall_confidence=result.get("overall_confidence", 1.0),
            needs_human_review=result.get("needs_human_review", False),
            graph_context=ctx_meta,
        )
