"""How the graph retriever plugs into the multi-agent reviewer.

Old flow:  diff -> ContextRetriever (embeddings + pgvector + reranker) -> one blob of chunks
New flow:  diff -> GraphContextRetriever (Cypher)                      -> one view per agent

`call_llm(system, user) -> str` is whatever you already use (LangGraph node,
OpenAI/Anthropic SDK, ...). Only the context-building part changes.
"""
from concurrent.futures import ThreadPoolExecutor

from src.pr_reviewer.graph.render import render_context
from src.pr_reviewer.graph.retriever import GraphContextRetriever

CITE_RULE = (
    "You are given facts from a code knowledge graph (FalkorDB). Every finding must cite the "
    "graph evidence it rests on (function, file:lines, hops) and say what could break. "
    "Do not report issues you cannot tie to the diff or the graph facts. "
    "If a fact says 'AMBIGUOUS' or the budget was truncated, say so instead of guessing."
)

AGENTS = {
    # role -> (render preset, focus instruction)
    "impact":    ("impact",    "Find callers/importers that this change can break (signature, return type, side effects, behaviour)."),
    "tests":     ("tests",     "Judge test coverage: which changed functions have no reaching test, and which tests need updating."),
    "contracts": ("contracts", "Check the new/changed code uses its dependencies correctly (arguments, return values, errors)."),
    "security":  ("security",  "Look for security and correctness problems in the changed code and what it calls."),
}


def review_pr(diff: str, retriever: GraphContextRetriever, call_llm) -> str:
    ctx = retriever.retrieve(diff)                       # <- the only retrieval step

    def run(item):
        role, (preset, focus) = item
        graph_facts = render_context(ctx, preset)
        user = f"## PR diff\n{diff}\n\n{graph_facts or '(no graph context for this diff)'}"
        return role, call_llm(f"{CITE_RULE}\n\nYour job: {focus}", user)

    with ThreadPoolExecutor(max_workers=len(AGENTS)) as pool:
        findings = dict(pool.map(run, AGENTS.items()))

    synth_user = "\n\n".join(f"### {role} agent\n{text}" for role, text in findings.items())
    return call_llm(
        "Merge these reviews into one PR review: deduplicate, rank by severity, keep each "
        "finding's graph evidence.",
        f"{render_context(ctx, ('summary',))}\n\n{synth_user}",
    )


if __name__ == "__main__":
    r = GraphContextRetriever.from_env(graph_name="code_graph")
    r.ensure_indexes()
    # print(review_pr(open("pr.diff").read(), r, call_llm=...))
