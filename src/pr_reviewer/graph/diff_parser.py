"""Unified diff -> per-file changes with the *exact* old-side lines touched.

The graph is built from the base branch, so a Function node's
start_line/end_line are OLD-side line numbers. To find which functions a PR
modifies we therefore need old-side line numbers of what actually changed,
not the hunk's header range (which includes ~3 lines of context on each side
and would wrongly pull in neighbouring functions).

Range convention (inclusive, old-side):
  * removed line n            -> (n, n)
  * insertion after old line n -> (n + 1, n)     # lo > hi marks an insertion
A function covering [start, end] is hit iff  start <= hi  AND  end >= lo.
For an insertion that is true only when it lands strictly inside the function
(start <= n and end >= n + 1), which is exactly what we want.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    path: str                        # path after the change
    old_path: str | None             # path in the base branch (None for new files)
    is_new: bool = False
    is_deleted: bool = False
    ranges: list[tuple[int, int]] = field(default_factory=list)  # old-side, see module doc
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def graph_path(self) -> str | None:
        """Path to look up in the graph (the base-branch path)."""
        return None if self.is_new else (self.old_path or self.path)


def _coalesce(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(set(ranges)):
        if out and lo <= hi and out[-1][0] <= out[-1][1] and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def parse_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    in_hunk = False
    old_ln = 0

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_HEADER.match(line)
            cur = FileDiff(path=m.group(2), old_path=m.group(1)) if m else None
            if cur:
                files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue

        if not in_hunk:
            # Header lines. Only inspected outside hunks, because a removed
            # line like "-- comment" would look like a "--- " header inside one.
            if line.startswith("new file mode"):
                cur.is_new, cur.old_path = True, None
            elif line.startswith("deleted file mode"):
                cur.is_deleted = True
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from "):].strip()
            elif line.startswith("rename to "):
                cur.path = line[len("rename to "):].strip()
            elif line.startswith("--- "):
                if "/dev/null" in line:
                    cur.is_new, cur.old_path = True, None
            elif line.startswith("+++ "):
                target = line[4:].split("\t")[0].strip()
                if target == "/dev/null":
                    cur.is_deleted = True
                elif target.startswith("b/"):
                    cur.path = target[2:]

        m = _HUNK.match(line)
        if m:
            in_hunk = True
            old_ln = int(m.group(1))
            continue
        if not in_hunk:
            continue

        if line.startswith("+"):
            cur.added.append(line[1:])
            cur.ranges.append((old_ln, old_ln - 1))   # inserted after old line old_ln-1
        elif line.startswith("-"):
            cur.removed.append(line[1:])
            cur.ranges.append((old_ln, old_ln))
            old_ln += 1
        elif line.startswith("\\"):
            pass                                       # "\ No newline at end of file"
        else:
            old_ln += 1                                # context line

    for f in files:
        f.ranges = _coalesce(f.ranges)
    return files