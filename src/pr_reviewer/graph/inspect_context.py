"""Show exactly which graph context the agents get for a code change.

  python -m src.pr_reviewer.graph.inspect_context --repo owner/name --pr 4
  python -m src.pr_reviewer.graph.inspect_context --repo owner/name --diff change.diff
  git diff main | python -m src.pr_reviewer.graph.inspect_context --repo owner/name --diff -

Options: --agent security|quality|tests|docs (show one agent), --facts (summary only)
"""
import argparse
import json
import os
import sys

import requests

from src.pr_reviewer.config import get_settings
from src.pr_reviewer.graph.agent_context import build_agent_contexts, describe
from src.pr_reviewer.graph.store import GraphStore


def _diff_from_github(repo: str, pr: int) -> str:
    headers = {"Accept": "application/vnd.github.v3.diff", "X-GitHub-Api-Version": "2022-11-28"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    r = requests.get(f"https://api.github.com/repos/{repo}/pulls/{pr}", headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--diff", help="diff file, or - for stdin")
    ap.add_argument("--agent")
    ap.add_argument("--facts", action="store_true")
    a = ap.parse_args()
    if not (a.pr or a.diff):
        ap.error("give --pr or --diff")

    diff = _diff_from_github(a.repo, a.pr) if a.pr else (
        sys.stdin.read() if a.diff == "-" else open(a.diff, encoding="utf-8").read())

    s = get_settings()
    store = GraphStore(host=s.falkordb_host, port=s.falkordb_port,
                       password=s.falkordb_password, fixed_graph_name=s.graph_name)
    ctx = store.context(a.repo, diff)

    print(f"graph: {store.graph_name(a.repo)}   diff: {len(diff)} chars")
    print("\n=== WHAT THE GRAPH FOUND ===")
    print(json.dumps(describe(ctx), indent=2))
    if ctx.is_empty:
        print("\n(context is EMPTY: agents get the diff only. Typical reasons: all files are new, "
              "changes are outside functions, or the graph was built from a different branch.)")
        return
    if a.facts:
        return

    generic, agents = build_agent_contexts(ctx)
    print(f"\n=== GENERIC FALLBACK ({len(generic)} chars) ===" if not a.agent else "")
    for name, text in agents.items():
        if a.agent and name != a.agent:
            continue
        print(f"\n{'=' * 8} AGENT: {name}  ({len(text)} chars) {'=' * 8}\n{text}")
    missing = [n for n in ("security", "quality", "tests", "docs") if n not in agents]
    if missing and not a.agent:
        print(f"\n(no graph context for: {', '.join(missing)}; they fall back to the generic text)")


if __name__ == "__main__":
    main()
