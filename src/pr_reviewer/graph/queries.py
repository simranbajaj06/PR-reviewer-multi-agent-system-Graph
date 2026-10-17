"""Every Cypher query the PR reviewer depends on (copy these into the README).

Graph schema (from build_graph2.py):
    (File)-[:DEFINES]->(Function)
    (Function)-[:CALLS]->(Function)
    (File)-[:IMPORTS]->(File)
    File:     path, name, language, is_test, content
    Function: id ("path::Class.method"), name, file, language, start_line, end_line, code
"""

# Indexes build_graph2.py does not create yet. Our lookups filter on these.
CREATE_INDEXES = (
    "CREATE INDEX FOR (fn:Function) ON (fn.file)",
    "CREATE INDEX FOR (fn:Function) ON (fn.name)",
)

_FN_COLS = (
    "c.id AS id, c.name AS name, c.file AS file, "
    "c.start_line AS start_line, c.end_line AS end_line, c.code AS code"
)

# 1. Diff -> graph: which existing functions overlap the changed old-side lines?
#    $ranges = [{file, lo, hi}, ...]  (see diff_parser for the lo/hi convention)
CHANGED_FUNCTIONS = f"""
UNWIND $ranges AS r
MATCH (c:Function {{file: r.file}})
WHERE c.start_line <= r.hi AND c.end_line >= r.lo
RETURN DISTINCT {_FN_COLS}
"""

# 2. Direct fan-in: how many functions call this one?
FAN_IN = """
MATCH (c:Function)-[:CALLS]->(f:Function {id: $id})
RETURN count(DISTINCT c) AS n
"""


def _depth(depth: int) -> int:
    d = int(depth)
    if not 1 <= d <= 6:
        raise ValueError("depth must be between 1 and 6")
    return d


# 3. Blast radius size: distinct production functions that reach this one in <= depth hops.
def reach_count(depth: int) -> str:
    return f"""
MATCH (c:Function)-[:CALLS*1..{_depth(depth)}]->(f:Function {{id: $id}})
MATCH (cf:File)-[:DEFINES]->(c)
WHERE NOT cf.is_test
RETURN count(DISTINCT c) AS n
"""


# 4. Blast radius: production callers within <= depth hops, nearest first.
def callers(depth: int) -> str:
    return f"""
MATCH p = (c:Function)-[:CALLS*1..{_depth(depth)}]->(f:Function {{id: $id}})
MATCH (cf:File)-[:DEFINES]->(c)
WHERE NOT cf.is_test
RETURN {_FN_COLS}, min(length(p)) AS dist
ORDER BY dist, file, start_line
LIMIT $limit
"""


# 5. Dependencies: what the changed function calls directly.
CALLEES = f"""
MATCH (f:Function {{id: $id}})-[:CALLS]->(c:Function)
RETURN {_FN_COLS}
ORDER BY file, start_line
LIMIT $limit
"""


# 6. Test coverage: test functions that reach this function in <= depth hops.
def tests(depth: int) -> str:
    return f"""
MATCH (tf:File)-[:DEFINES]->(c:Function)-[:CALLS*1..{_depth(depth)}]->(f:Function {{id: $id}})
WHERE tf.is_test
RETURN DISTINCT {_FN_COLS}
ORDER BY file, start_line
LIMIT $limit
"""


# 7. Module-level impact: which files import a changed file?
IMPORTERS = """
MATCH (a:File)-[:IMPORTS]->(b:File {path: $path})
RETURN a.path AS path, a.is_test AS is_test
ORDER BY path
LIMIT $limit
"""

# 8. Symbols used by NEW code: resolve called names to existing definitions.
#    $names comes from the added diff lines (e.g. "audit_log(" -> "audit_log").
SYMBOL_LOOKUP = f"""
UNWIND $names AS n
MATCH (c:Function {{name: n}})
RETURN n AS ref, {_FN_COLS}
LIMIT $limit
"""
