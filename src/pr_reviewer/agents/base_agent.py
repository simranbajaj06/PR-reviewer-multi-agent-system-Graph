"""Shared specialist-agent machinery. Each concrete agent module
(security_agent.py, quality_agent.py, ...) supplies only a system prompt
and an AgentType; this module owns the LLM call, structured-output
parsing, and defensive agent_type stamping that every specialist needs
identically. Adding a 5th specialist is "one new file with a prompt",
never a copy-paste of this plumbing.
"""
import logging
import random
import threading
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from pydantic import ValidationError

from src.pr_reviewer.domain.contracts import AgentType, Severity, Finding, SpecialistOutput
from src.pr_reviewer.orchestrator.state import ReviewState

logger = logging.getLogger(__name__)

# Keeps the diff well inside the model's context window. Large PRs need real
# chunking/retrieval later (the grounding problem) rather than a hard
# truncate, but truncating is the honest MVP behavior for now.
MAX_DIFF_CHARS = 40_000

# How many times to retry a single specialist call on a malformed/failed
# generation before giving up and degrading gracefully for that specialist
# only. Keeps one flaky call from taking down the whole review.
MAX_RETRIES = 4

# Base wait (seconds) for exponential back-off on rate-limit errors.
# Actual wait = BASE_BACKOFF * 2^(attempt-1) + jitter(0..1s).
BASE_BACKOFF = 12.0

# Stagger agents so they don't all send tokens at the same instant.
# Key order in _AGENT_BUILDERS determines the index passed here.
_STAGGER_LOCK = threading.Lock()
_STAGGER_SLOTS: dict[str, int] = {}   # agent_name -> slot index


def _slot(agent_type_value: str) -> int:
    with _STAGGER_LOCK:
        if agent_type_value not in _STAGGER_SLOTS:
            _STAGGER_SLOTS[agent_type_value] = len(_STAGGER_SLOTS)
        return _STAGGER_SLOTS[agent_type_value]


def make_specialist_node(agent_type: AgentType, system_prompt: str, llm: ChatGroq):
    """Returns a LangGraph node function bound to one specialist's prompt
    and agent_type. All specialist modules call this - they never touch
    the LLM or the graph state directly.

    Uses JSON mode rather than tool-calling for structured output. Some
    Groq-hosted models (notably openai/gpt-oss-120b) use a "Harmony"
    response format with built-in tool channels, and on code-heavy diffs
    can hallucinate a tool call to a phantom tool named after the
    programming language in the diff (e.g. "csharp") instead of the
    SpecialistOutput schema tool. JSON mode has no tool namespace for the
    model to hallucinate into, so it sidesteps the bug entirely. Per Groq's
    JSON mode requirement, the word "json" must appear in the prompt.
    """
    structured_llm = llm.with_structured_output(SpecialistOutput, method="json_mode")

    def node(state: ReviewState) -> dict:
        diff = state["diff"]
        # Each specialist gets its own slice of the code graph (see
        # graph/render.py ROLE_PRESETS); fall back to the generic context.
        context = (state.get("agent_context") or {}).get(agent_type.value) \
            or state.get("context", "")
        # Graph facts are kept clearly separate from the diff and labelled
        # read-only, so findings stay anchored to the change.
        context_note = (
            "\n\nYou may also be given 'Code graph context': facts about the repository's "
            "base branch taken from a knowledge graph (who calls the changed functions, "
            "what they call, which tests reach them, who imports the changed files, and "
            "existing functions the new code uses). It describes code as it was BEFORE "
            "this change. Rules: (1) every finding must be anchored to a file/line that "
            "is in the diff; (2) when the problem is that code OUTSIDE the diff would "
            "break or behave differently (e.g. a caller of a function whose signature "
            "changed), anchor the finding on the changed line and name the affected "
            "function, its file:lines and the hop distance in the rationale; (3) never "
            "report issues that exist only in the graph context; (4) do not assume "
            "the graph is complete: a missing caller or test only means none was found."
        ) if context else ""
        context_section = (
            "\n\nCode graph context (base branch, NOT part of this change):\n\n" + context
        ) if context else ""
        messages = [
            SystemMessage(
                content=f"{system_prompt}{context_note}\n\n"
                        f"Respond with a single JSON object matching this schema: "
                        f'{{"findings": [{{"agent_type": "{agent_type.value}", '
                        f'"severity": "critical|high|medium|low|info", "category": str, '
                        f'"summary": str, "file": str|null, "line": int|null, '
                        f'"confidence": float (0-1), "rationale": str, '
                        f'"suggestion": str|null}}]}}. '
                        f"Return {{\"findings\": []}} if there is nothing to report."
            ),
            HumanMessage(
                content=f"PR #{state['pr_number']}: {state['pr_title']}\n\n"
                        f"Diff (language of the changed files may vary):\n\n"
                        f"{diff}{context_section}"
            ),
        ]

        last_error: Exception | None = None
        start = time.monotonic()

        # Stagger agents: slot 0 fires immediately, slot 1 waits ~3 s,
        # slot 2 ~6 s, slot 3 ~9 s.  Keeps 4 simultaneous calls from
        # flooding the 8 k TPM budget in the same window.
        stagger_delay = _slot(agent_type.value) * 3.0
        if stagger_delay:
            logger.info("specialist=%s staggering %.0fs to spread TPM load",
                        agent_type.value, stagger_delay)
            time.sleep(stagger_delay)

        logger.info(
            "specialist=%s status=start thread=%s",
            agent_type.value, threading.current_thread().name,
        )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result: SpecialistOutput = structured_llm.invoke(messages)
                # Stamp agent_type defensively in case the model omits/
                # mislabels it despite the schema.
                findings = [
                    f.model_copy(update={"agent_type": agent_type}) for f in result.findings
                ]
                logger.info(
                    "specialist=%s status=done thread=%s elapsed=%.2fs findings=%d",
                    agent_type.value, threading.current_thread().name,
                    time.monotonic() - start, len(findings),
                )
                return {"findings": findings}
            except Exception as exc:  # noqa: BLE001 - deliberately broad
                last_error = exc
                err_str = str(exc)
                is_rate_limit = "429" in err_str or "rate_limit" in err_str.lower()
                if is_rate_limit and attempt < MAX_RETRIES:
                    wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning(
                        "specialist=%s attempt=%d/%d rate-limited; backing off %.1fs: %s",
                        agent_type.value, attempt, MAX_RETRIES, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "specialist=%s attempt=%d/%d failed: %s",
                        agent_type.value, attempt, MAX_RETRIES, exc,
                    )

        # Every retry failed. Degrade gracefully: surface one LOW-confidence
        # INFO finding noting the specialist didn't run, rather than crashing
        # the whole graph and losing the other three specialists' findings.
        logger.error(
            "specialist=%s status=failed thread=%s elapsed=%.2fs attempts=%d: %s",
            agent_type.value, threading.current_thread().name,
            time.monotonic() - start, MAX_RETRIES, last_error,
        )
        return {
            "findings": [
                Finding(
                    agent_type=agent_type,
                    severity=Severity.INFO,
                    category="agent-error",
                    summary=f"The {agent_type.value} review could not complete.",
                    confidence=0.0,
                    rationale=f"LLM call failed after {MAX_RETRIES} attempts: {last_error}",
                )
            ]
        }

    return node