"""Which (file, line) pairs can GitHub accept as inline review comments?

GitHub only allows an inline comment on a line that appears in the PR diff
(added or unchanged-context lines on the new side). If even ONE comment points
elsewhere, the whole review request fails with 422. LLMs often cite lines that
are not in the diff, so we filter before posting.
"""
import re

_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """path -> set of new-side line numbers that are inside a diff hunk."""
    out: dict[str, set[int]] = {}
    cur: set[int] | None = None
    in_hunk = False
    new_ln = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = _FILE.match(line)
            cur = out.setdefault(m.group(2), set()) if m else None
            in_hunk = False
            continue
        if cur is None:
            continue
        m = _HUNK.match(line)
        if m:
            new_ln, in_hunk = int(m.group(1)), True
            continue
        if not in_hunk or line.startswith("\\"):   # "\ No newline at end of file"
            continue
        if line.startswith("-"):
            continue                                # old side only
        cur.add(new_ln)                             # "+" or context line
        new_ln += 1
    return out
