"""tests/graph/test_graph_retriever.py

Parser tests need nothing. Retriever tests run real Cypher against an embedded
FalkorDB (pip install falkordblite) and are skipped if it isn't installed.
"""
import pytest

from src.pr_reviewer.graph.diff_parser import parse_diff

DIFF = '''\
diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -15,3 +15,4 @@ def handler(req):
     user = load(req)
-    return render(user)
+    audit_log(user, req.headers)
+    return render(user, strict=True)
@@ -70,2 +71,3 @@ class Cache:
     x = 1
+    self._ttl_seconds = compute_ttl(config.cache_settings)
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+def fresh():
+    return 1
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    return "removed"
'''


def test_parse_diff_exact_old_side_ranges():
    a, new, old = parse_diff(DIFF)
    # context lines (15, 70) are NOT part of the change
    assert a.ranges == [(16, 16), (17, 16), (71, 70)]
    assert "audit_log(user, req.headers)" in a.added[0]
    assert new.is_new and new.graph_path is None
    assert old.is_deleted and old.ranges == [(1, 2)]


def test_dash_comment_inside_hunk_is_not_a_header():
    d = ("diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n"
         "--- removed sql comment\n keep\n")
    (f,) = parse_diff(d)
    assert f.removed == ["-- removed sql comment"] and f.path == "q.sql"


# ---------------------------------------------------------------- real Cypher

falkor = pytest.importorskip("redislite.falkordb_client")

from src.pr_reviewer.graph.retriever import GraphContextRetriever  # noqa: E402
from src.pr_reviewer.graph.render import render_context  # noqa: E402


@pytest.fixture()
def retriever(tmp_path):
    g = falkor.FalkorDB(str(tmp_path / "t.db")).select_graph("t")
    g.query("""
    CREATE (u:File {path:'app/u.py', is_test:false}), (a:File {path:'app/a.py', is_test:false}),
           (t:File {path:'tests/test_a.py', is_test:true}),
           (h:Function {id:'app/u.py::hash', name:'hash', file:'app/u.py', start_line:1, end_line:2, code:'def hash(p):\\n    return p'}),
           (v:Function {id:'app/a.py::validate', name:'validate', file:'app/a.py', start_line:1, end_line:2, code:'def validate(p):\\n    return hash(p)'}),
           (o:Function {id:'app/a.py::other', name:'other', file:'app/a.py', start_line:4, end_line:5, code:'def other():\\n    return 1'}),
           (x:Function {id:'tests/test_a.py::test_v', name:'test_v', file:'tests/test_a.py', start_line:1, end_line:2, code:'def test_v():\\n    validate(1)'}),
           (u)-[:DEFINES]->(h), (a)-[:DEFINES]->(v), (a)-[:DEFINES]->(o), (t)-[:DEFINES]->(x),
           (v)-[:CALLS]->(h), (x)-[:CALLS]->(v), (a)-[:IMPORTS]->(u)
    """)
    return GraphContextRetriever(g)


HASH_DIFF = '''\
diff --git a/app/u.py b/app/u.py
--- a/app/u.py
+++ b/app/u.py
@@ -1,2 +1,2 @@
-def hash(p):
-    return p
+def hash(p, salt):
+    return salt + p
'''


def test_blast_radius_tests_and_importers(retriever):
    ctx = retriever.retrieve(HASH_DIFF)
    (cf,) = ctx.changed
    assert cf.fn.id == "app/u.py::hash"
    assert cf.signature_touched and cf.fan_in == 1
    assert [(c.name, c.dist) for c in cf.callers] == [("validate", 1)]
    assert [t.name for t in cf.tests] == ["test_v"]          # test -> validate -> hash
    assert ctx.importers == {"app/u.py": ["app/a.py"]}
    assert cf.risk == "high"
    assert "SIGNATURE CHANGED" in render_context(ctx, "impact")


def test_insertion_between_functions_hits_nothing(retriever):
    # inserted after old line 3 of app/a.py: between validate (1-2) and other (4-5)
    d = ("diff --git a/app/a.py b/app/a.py\n--- a/app/a.py\n+++ b/app/a.py\n"
         "@@ -3,0 +4,1 @@\n+X = 1\n")
    ctx = retriever.retrieve(d)
    assert ctx.changed == [] and ctx.module_level_files == ["app/a.py"]


def test_new_code_references_resolved(retriever):
    d = ("diff --git a/app/a.py b/app/a.py\n--- a/app/a.py\n+++ b/app/a.py\n"
         "@@ -4,2 +4,3 @@ def other():\n def other():\n+    validate(2)\n     return 1\n")
    ctx = retriever.retrieve(d)
    assert [c.fn.name for c in ctx.changed] == ["other"]
    assert "validate" in ctx.references
