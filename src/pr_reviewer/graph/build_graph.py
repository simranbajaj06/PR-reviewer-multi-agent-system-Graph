"""
build_graph.py - Turn ANY GitHub repository (any language) into a FalkorDB graph.

Graph schema:
    (File)-[:DEFINES]->(Function)
    (Function)-[:CALLS]->(Function)
    (File)-[:IMPORTS]->(File)

Node properties:
    File:     path, name, language, is_test, content
    Function: id, name, file, language, start_line, end_line, code

How it works (language independent):
    1. Parse every source file with tree-sitter (supports many languages).
    2. A small table (LANGUAGE_RULES) says which syntax nodes mean
       "function", "class", "call" and "import" in each language.
    3. One generic algorithm walks the syntax tree using that table.

To support a new language: add its file extension to CODE_EXTENSIONS and
one entry to LANGUAGE_RULES (the grammar ships with tree-sitter-language-pack).

Usage:
    python build_graph.py <github_url_or_local_folder> [graph_name]
"""

import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

# pyrefly: ignore [missing-import]
from falkordb import FalkorDB
# pyrefly: ignore [missing-import]
from tree_sitter_language_pack import get_parser

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", "vendor", ".idea", ".next", "coverage", "out",
}
MAX_FILE_CHARS = 100_000    # cap on File.content
MAX_CODE_CHARS = 20_000     # cap on Function.code
MAX_PARSE_BYTES = 500_000   # skip parsing huge (usually generated) files

# File extension -> language name used by tree-sitter
CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp",
}

# Files we store as File nodes (with content) but do not parse for functions
CONFIG_EXTENSIONS = {".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json"}

# ---------------------------------------------------------------------------
# Per-language rules: which syntax nodes mean what
# ---------------------------------------------------------------------------

JS_FUNCTIONS = {"function_declaration", "generator_function_declaration", "method_definition"}
JS_CLASSES = {"class_declaration", "class"}
JS_IMPORTS = {"import_statement", "export_statement"}  # export ... from "x"

LANGUAGE_RULES = {
    "python": {
        "functions": {"function_definition"},
        "classes": {"class_definition"},
        "calls": {"call"},
        "imports": {"import_statement", "import_from_statement"},
    },
    "javascript": {"functions": JS_FUNCTIONS, "classes": JS_CLASSES,
                   "calls": {"call_expression"}, "imports": JS_IMPORTS},
    "typescript": {"functions": JS_FUNCTIONS, "classes": JS_CLASSES | {"interface_declaration"},
                   "calls": {"call_expression"}, "imports": JS_IMPORTS},
    "tsx": {"functions": JS_FUNCTIONS, "classes": JS_CLASSES | {"interface_declaration"},
            "calls": {"call_expression"}, "imports": JS_IMPORTS},
    "java": {
        "functions": {"method_declaration", "constructor_declaration"},
        "classes": {"class_declaration", "interface_declaration", "enum_declaration"},
        "calls": {"method_invocation"},
        "imports": {"import_declaration"},
    },
    "go": {
        "functions": {"function_declaration", "method_declaration"},
        "classes": set(),
        "calls": {"call_expression"},
        "imports": {"import_spec"},
    },
    "rust": {
        "functions": {"function_item"},
        "classes": {"impl_item", "struct_item", "trait_item"},
        "calls": {"call_expression"},
        "imports": {"use_declaration", "mod_item"},
    },
    "ruby": {
        "functions": {"method", "singleton_method"},
        "classes": {"class", "module"},
        "calls": {"call"},
        "imports": set(),  # Ruby imports are calls to require / require_relative
    },
    "php": {
        "functions": {"function_definition", "method_declaration"},
        "classes": {"class_declaration", "interface_declaration", "trait_declaration"},
        "calls": {"function_call_expression", "member_call_expression", "scoped_call_expression"},
        "imports": {"namespace_use_declaration"},
    },
    "c": {
        "functions": {"function_definition"},
        "classes": set(),
        "calls": {"call_expression"},
        "imports": {"preproc_include"},
    },
    "cpp": {
        "functions": {"function_definition"},
        "classes": {"class_specifier", "struct_specifier"},
        "calls": {"call_expression"},
        "imports": {"preproc_include"},
    },
    "csharp": {
        "functions": {"method_declaration", "constructor_declaration"},
        "classes": {"class_declaration", "interface_declaration", "struct_declaration"},
        "calls": {"invocation_expression"},
        "imports": {"using_directive"},
    },
}

JS_LIKE = {"javascript", "typescript", "tsx"}
# Languages that can import each other (a .ts file may import a .js file, etc.)
LANGUAGE_FAMILY = {"javascript": "js", "typescript": "js", "tsx": "js", "c": "c", "cpp": "c"}
ARROW_FUNCTION_TYPES = {"arrow_function", "function_expression", "function"}
REQUIRE_NAMES = {"require", "require_relative"}  # calls that act as imports
IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


# ---------------------------------------------------------------------------
# Step 1: Get the repo and list its files
# ---------------------------------------------------------------------------

def clone_repo(url, target_dir):
    """Shallow clone: we only need the latest code, not the history."""
    subprocess.run(["git", "clone", "--depth", "1", url, target_dir], check=True)


def get_language(filename):
    """Return (kind, language) for files we care about, or None to skip."""
    lower = filename.lower()
    if lower == "dockerfile" or lower.startswith("dockerfile."):
        return "config", "dockerfile"
    ext = os.path.splitext(lower)[1]
    if ext in CODE_EXTENSIONS:
        if lower.endswith(".min.js"):
            return None  # minified bundles are noise
        return "code", CODE_EXTENSIONS[ext]
    if ext in CONFIG_EXTENSIONS and not lower.endswith("-lock.json"):
        return "config", CONFIG_EXTENSIONS[ext]
    return None


def is_test_file(path):
    """A file is a test if it sits in a test folder or is named like a test."""
    lower = path.lower()
    parts = lower.split("/")
    name = parts[-1]
    in_test_dir = any(p in ("test", "tests", "__tests__", "spec", "specs") for p in parts[:-1])
    test_name = (
        name.startswith("test_")
        or re.search(r"(_test|\.test|\.spec|_spec)\.\w+$", name) is not None
        or re.search(r"tests?\.(java|cs)$", name) is not None
    )
    return in_test_dir or test_name


def read_bytes(full_path):
    with open(full_path, "rb") as f:
        return f.read()


def scan_repo(repo_dir):
    """Walk the repo and return one record per file we care about."""
    files = []
    for root, dirs, names in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            found = get_language(name)
            if found is None:
                continue
            kind, language = found
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, repo_dir).replace(os.sep, "/")
            raw = read_bytes(full_path)
            files.append({
                "path": rel_path,
                "name": name,
                "language": language,
                "kind": kind,
                "is_test": is_test_file(rel_path),
                "content": raw.decode("utf-8", errors="ignore")[:MAX_FILE_CHARS],
                "raw": raw,  # kept for parsing, not stored in the graph
            })
    return files


# ---------------------------------------------------------------------------
# Step 2: Parse one source file with tree-sitter
# ---------------------------------------------------------------------------

_parsers = {}


def parser_for(language):
    if language not in _parsers:
        _parsers[language] = get_parser(language)
    return _parsers[language]


def text_of(node):
    return node.text.decode("utf-8", errors="ignore")


def last_identifier(text):
    """'a.b.c' -> 'c',  'Foo::bar' -> 'bar',  '*foo' -> 'foo'."""
    words = IDENTIFIER.findall(text)
    return words[-1] if words else None


def get_name(node):
    """Name of a function or class. Falls back to the C-style 'declarator'."""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return last_identifier(text_of(name_node))
    declarator = node.child_by_field_name("declarator")  # C / C++ functions
    if declarator is not None:
        return last_identifier(text_of(declarator).split("(")[0])
    type_node = node.child_by_field_name("type")  # Rust: impl Foo { ... }
    if type_node is not None:
        return last_identifier(text_of(type_node))
    return None


def function_info(node, language, rules):
    """
    If this syntax node is a function, return (name, node_covering_its_code).
    Otherwise return None.
    """
    if node.type in rules["functions"]:
        # Include decorators (@app.route ...) as part of a Python function
        covering = node.parent if node.parent and node.parent.type == "decorated_definition" else node
        return get_name(node), covering

    # JavaScript / TypeScript: functions stored in a variable, property or object key
    #     const add = (a, b) => a + b        app.get = function () {}        { run: () => {} }
    if language in JS_LIKE:
        value = name = None
        covering = node
        if node.type == "variable_declarator":
            value, name = node.child_by_field_name("value"), get_name(node)
            if node.parent and node.parent.type in ("lexical_declaration", "variable_declaration"):
                covering = node.parent
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            value = node.child_by_field_name("right")
            name = last_identifier(text_of(left)) if left is not None else None
            if node.parent and node.parent.type == "expression_statement":
                covering = node.parent
        elif node.type == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            name = last_identifier(text_of(key)) if key is not None else None
        if value is not None and value.type in ARROW_FUNCTION_TYPES:
            return name, covering
    return None


def call_name(node):
    """Name of the function being called: `a.b.run(x)` -> 'run'."""
    target = (node.child_by_field_name("method")
              or node.child_by_field_name("name")
              or node.child_by_field_name("function"))
    if target is None:
        return None
    text = text_of(target)
    if len(text) > 100:  # a long expression, not a simple name
        return None
    return last_identifier(text)


def strip_quotes(text):
    return text.strip().strip("\"'`<>")


def extract_imports(node, language):
    """Return the import 'specs' (module names / paths) found in an import node."""
    text = text_of(node).strip()

    if language == "python":
        match = re.match(r"from\s+(\S+)\s+import\s+(.+)", text, re.S)
        if match:
            module = match.group(1)
            names = re.sub(r"\s+as\s+\w+", "", match.group(2))
            names = [n for n in re.split(r"[,\s()]+", names) if n and n != "*"]
            joiner = "" if module.endswith(".") else "."
            return [module] + [module + joiner + n for n in names]  # a name may be a module
        match = re.match(r"import\s+(.+)", text, re.S)
        if match:
            parts = re.sub(r"\s+as\s+\w+", "", match.group(1)).split(",")
            return [p.strip() for p in parts if p.strip()]
        return []

    if language in JS_LIKE:
        source = node.child_by_field_name("source")
        return [strip_quotes(text_of(source))] if source is not None else []

    if language == "go":
        path = node.child_by_field_name("path")
        return [strip_quotes(text_of(path))] if path is not None else []

    if language in ("c", "cpp"):
        path = node.child_by_field_name("path")
        return [strip_quotes(text_of(path))] if path is not None else []

    if language == "rust":
        if node.type == "mod_item":  # `mod foo;` (a module in another file)
            return [] if node.child_by_field_name("body") else [get_name(node)]
        spec = re.sub(r"^(pub\s+)?use\s+", "", text).rstrip(";").strip()
        return [spec.split("::{")[0].split(" as ")[0]]

    if language in ("java", "csharp", "php"):
        spec = re.sub(r"^(import|using|use)\s+(static\s+)?", "", text).rstrip(";").strip()
        return [spec.split(" as ")[0]]

    return []


def parse_source_file(file):
    """
    Return the functions, calls and imports found in one source file.
    Walks the syntax tree once, remembering the current class and function.
    """
    language, path = file["language"], file["path"]
    rules = LANGUAGE_RULES.get(language)
    if rules is None or len(file["raw"]) > MAX_PARSE_BYTES:
        return {"functions": [], "calls": [], "imports": []}

    tree = parser_for(language).parse(file["raw"])
    lines = file["raw"].decode("utf-8", errors="ignore").splitlines()

    functions, calls, imports = [], [], []
    seen_ids = set()

    # Each stack item: (syntax node, enclosing class name, enclosing function id)
    stack = [(tree.root_node, None, None)]
    while stack:
        node, class_name, fn_id = stack.pop()

        if node.type in rules["classes"]:
            class_name = get_name(node) or class_name

        info = function_info(node, language, rules)
        if info and info[0]:
            name, covering = info
            qualified = f"{class_name}.{name}" if class_name else name
            new_id = f"{path}::{qualified}"
            if new_id in seen_ids:  # overloads or duplicate names in one file
                new_id = f"{new_id}@{covering.start_point[0] + 1}"
            seen_ids.add(new_id)

            start_row, end_row = covering.start_point[0], covering.end_point[0]
            functions.append({
                "id": new_id,
                "name": name,
                "class_name": class_name,
                "file": path,
                "language": language,
                "start_line": start_row + 1,
                "end_line": end_row + 1,
                "code": "\n".join(lines[start_row:end_row + 1]),
            })
            fn_id = new_id

        elif node.type in rules["calls"]:
            name = call_name(node)
            if name in REQUIRE_NAMES:  # require("./x")  /  require_relative "x"
                match = re.search(r"""["']([^"']+)["']""", text_of(node))
                if match:
                    spec = match.group(1)
                    if name == "require_relative" and not spec.startswith("."):
                        spec = "./" + spec
                    imports.append(spec)
            elif name and fn_id:
                calls.append({"caller": fn_id, "class_name": class_name, "name": name})

        elif node.type in rules["imports"]:
            imports.extend(extract_imports(node, language))

        for child in reversed(node.children):
            stack.append((child, class_name, fn_id))

    return {"functions": functions, "calls": calls, "imports": imports}


# ---------------------------------------------------------------------------
# Step 3: Resolve imports (-> IMPORTS edges)
# ---------------------------------------------------------------------------

class FileIndex:
    """Lookup tables that let us find a repo file from an import string."""

    def __init__(self, code_files):
        code_paths = [f["path"] for f in code_files]
        self.paths = set(code_paths)
        self.family = {f["path"]: LANGUAGE_FAMILY.get(f["language"], f["language"]) for f in code_files}
        self.by_stem = defaultdict(list)    # "app/utils"      -> ["app/utils.py"]
        self.by_suffix = defaultdict(list)  # "utils", "app/utils" -> [...]
        self.by_dir = defaultdict(list)     # "pkg/util"       -> files in that folder

        for path in code_paths:
            stem = posixpath.splitext(path)[0]
            self.by_stem[stem].append(path)
            self.by_dir[posixpath.dirname(path)].append(path)

            parts = stem.split("/")
            variants = [parts]
            if parts[-1] in ("__init__", "index", "mod") and len(parts) > 1:
                variants.append(parts[:-1])  # the folder itself acts as the module
            for variant in variants:
                for i in range(len(variant)):
                    self.by_suffix["/".join(variant[i:])].append(path)


def pick_closest(from_path, candidates):
    """If several files match, keep the ones sharing the longest folder prefix."""
    if len(candidates) <= 1:
        return candidates

    def shared_folders(path):
        a, b = from_path.split("/")[:-1], path.split("/")[:-1]
        count = 0
        for x, y in zip(a, b):
            if x != y:
                break
            count += 1
        return count

    best = max(shared_folders(p) for p in candidates)
    top = [p for p in candidates if shared_folders(p) == best]
    return top if len(top) <= 2 else []  # still ambiguous -> give up


def same_family(paths, language, index):
    """Keep only files written in a language that can be imported from `language`."""
    wanted = LANGUAGE_FAMILY.get(language, language)
    return [p for p in paths if index.family[p] == wanted]


def lookup_relative(base, language, index):
    """Find a file for a path without extension, like 'src/utils'."""
    if base in index.paths:
        return [base]
    hits = same_family(index.by_stem.get(base, []), language, index)
    if hits:
        return hits[:1]
    for folder_file in ("index", "__init__", "mod"):
        hits = same_family(index.by_stem.get(f"{base}/{folder_file}", []), language, index)
        if hits:
            return hits[:1]
    return []


def resolve_import(from_path, language, spec, index):
    """Turn an import string into a list of repo file paths (maybe empty)."""
    spec = (spec or "").strip()
    if not spec:
        return []
    from_dir = posixpath.dirname(from_path)

    # "./utils" or "../lib/x"  (JavaScript, TypeScript, Ruby, C includes)
    if spec.startswith(("./", "../")):
        return lookup_relative(posixpath.normpath(posixpath.join(from_dir, spec)), language, index)

    # Python relative import: ".utils", "..pkg.mod"
    if language == "python" and spec.startswith("."):
        level = len(spec) - len(spec.lstrip("."))
        base_dir = from_dir
        for _ in range(level - 1):
            base_dir = posixpath.dirname(base_dir)
        rest = spec.lstrip(".").replace(".", "/")
        return lookup_relative(posixpath.join(base_dir, rest) if rest else base_dir, language, index)

    if language in ("c", "cpp"):  # "util.h" -> "util"; header and source share it
        spec = posixpath.splitext(spec)[0]
        local = lookup_relative(posixpath.normpath(posixpath.join(from_dir, spec)), language, index)
        if local:
            return local

    segments = [s for s in re.split(r"::|[./\\]", spec) if re.fullmatch(r"\w+", s)]
    segments = [s for s in segments if s not in ("crate", "self", "super")]
    if not segments:
        return []

    # Go imports a whole folder: "github.com/me/app/pkg/util" ends with the repo folder "pkg/util"
    if language == "go":
        for i in range(len(segments)):
            folder = "/".join(segments[i:])
            if folder in index.by_dir:
                return same_family(index.by_dir[folder], language, index)[:20]
        return []

    # Everything else: the import name is the end of a repo path.
    # "com.app.Foo" -> ".../com/app/Foo.java". Try shorter names too, because
    # the last part may be a function/class inside the module (a.b.func).
    for i in range(len(segments), 0, -1):
        found = same_family(index.by_suffix.get("/".join(segments[:i]), []), language, index)
        if found:
            return pick_closest(from_path, found)
    return []


# ---------------------------------------------------------------------------
# Step 4: Resolve calls (-> CALLS edges)
# ---------------------------------------------------------------------------

def resolve_calls(parsed, imported_files, test_files):
    """
    Match each call name to a function. In order of trust:
      1. a function in the same file (preferring the same class)
      2. a function in a file this file imports
      3. a function in the same folder
      4. the only function with that name in the whole repo
    If still ambiguous, no edge is created (better no edge than a wrong one).
    """
    by_name = defaultdict(list)
    for info in parsed.values():
        for fn in info["functions"]:
            by_name[fn["name"]].append(fn)

    edges = set()
    for path, info in parsed.items():
        folder = posixpath.dirname(path)
        imports = imported_files.get(path, set())

        for call in info["calls"]:
            candidates = by_name.get(call["name"], [])
            if path not in test_files:  # production code never calls test helpers
                candidates = [c for c in candidates if c["file"] not in test_files]
            callee = None

            same_file = [c for c in candidates if c["file"] == path]
            same_class = [c for c in same_file if call["class_name"] and c["class_name"] == call["class_name"]]
            if len(same_class) == 1:
                callee = same_class[0]
            elif len(same_file) == 1:
                callee = same_file[0]
            elif not same_file:
                for tier in (
                    [c for c in candidates if c["file"] in imports],
                    [c for c in candidates if posixpath.dirname(c["file"]) == folder],
                    candidates,
                ):
                    if len(tier) == 1:
                        callee = tier[0]
                        break

            if callee and callee["id"] != call["caller"]:
                edges.add((call["caller"], callee["id"]))

    return [{"caller": a, "callee": b} for a, b in edges]


# ---------------------------------------------------------------------------
# Step 5: Analyse the whole repo
# ---------------------------------------------------------------------------

def analyze_repo(repo_dir):
    files = scan_repo(repo_dir)
    code_files = [f for f in files if f["kind"] == "code"]
    index = FileIndex(code_files)

    parsed = {f["path"]: parse_source_file(f) for f in code_files}

    # IMPORTS edges, plus a per-file set used to resolve calls
    imported_files = defaultdict(set)
    for f in code_files:
        for spec in parsed[f["path"]]["imports"]:
            for target in resolve_import(f["path"], f["language"], spec, index):
                if target != f["path"]:
                    imported_files[f["path"]].add(target)

    imports = [{"from": a, "to": b} for a, targets in imported_files.items() for b in targets]
    functions = [fn for info in parsed.values() for fn in info["functions"]]
    test_files = {f["path"] for f in files if f["is_test"]}
    calls = resolve_calls(parsed, imported_files, test_files)

    for f in files:
        del f["raw"]  # no longer needed
    return files, functions, imports, calls


# ---------------------------------------------------------------------------
# Step 6: Load everything into FalkorDB
# ---------------------------------------------------------------------------

def run_in_batches(graph, query, rows, batch_size=100):
    """Send rows to FalkorDB in chunks using UNWIND (much faster than one by one)."""
    for i in range(0, len(rows), batch_size):
        graph.query(query, {"rows": rows[i:i + batch_size]})


def load_graph(graph, files, functions, imports, calls):
    for query in (
        "CREATE INDEX FOR (f:File) ON (f.path)",
        "CREATE INDEX FOR (fn:Function) ON (fn.id)",
        "CREATE INDEX FOR (fn:Function) ON (fn.file)",   # used by the reviewer's diff -> function lookup
        "CREATE INDEX FOR (fn:Function) ON (fn.name)",   # used by the reviewer's symbol lookup
    ):
        try:
            graph.query(query)
        except Exception:
            pass  # index already exists

    # File nodes (with their full content)
    run_in_batches(graph, """
        UNWIND $rows AS r
        MERGE (f:File {path: r.path})
        SET f.name = r.name, f.language = r.language,
            f.is_test = r.is_test, f.content = r.content
    """, files)

    # Function nodes (with their source code) + DEFINES edges
    run_in_batches(graph, """
        UNWIND $rows AS r
        MATCH (f:File {path: r.file})
        MERGE (fn:Function {id: r.id})
        SET fn.name = r.name, fn.file = r.file, fn.language = r.language,
            fn.start_line = r.start_line, fn.end_line = r.end_line, fn.code = r.code
        MERGE (f)-[:DEFINES]->(fn)
    """, functions)

    # IMPORTS edges (File -> File)
    run_in_batches(graph, """
        UNWIND $rows AS r
        MATCH (a:File {path: r.from}), (b:File {path: r.to})
        MERGE (a)-[:IMPORTS]->(b)
    """, imports)

    # CALLS edges (Function -> Function)
    run_in_batches(graph, """
        UNWIND $rows AS r
        MATCH (a:Function {id: r.caller}), (b:Function {id: r.callee})
        MERGE (a)-[:CALLS]->(b)
    """, calls)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python build_graph.py <github_url_or_local_folder> [graph_name]")
        sys.exit(1)

    source = sys.argv[1]
    graph_name = sys.argv[2] if len(sys.argv) > 2 else "code_graph"

    work_dir = None
    try:
        if os.path.isdir(source):
            repo_dir = source
        else:
            work_dir = tempfile.mkdtemp()
            print(f"Cloning {source} ...")
            clone_repo(source, work_dir)
            repo_dir = work_dir

        files, functions, imports, calls = analyze_repo(repo_dir)

        by_language = defaultdict(int)
        for fn in functions:
            by_language[fn["language"]] += 1
        print(f"Found {len(files)} files, functions per language: {dict(by_language)}")

        db = FalkorDB(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            password=os.getenv("FALKORDB_PASSWORD") or None,
        )
        try:
            db.select_graph(graph_name).delete()  # start from a clean graph
        except Exception:
            pass  # graph didn't exist yet
        graph = db.select_graph(graph_name)

        load_graph(graph, files, functions, imports, calls)

        print(f"Graph '{graph_name}' built:")
        print(f"  {len(files)} File nodes")
        print(f"  {len(functions)} Function nodes")
        print(f"  {len(imports)} IMPORTS edges")
        print(f"  {len(calls)} CALLS edges")
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()