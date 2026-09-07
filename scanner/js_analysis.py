"""
JavaScript gap analyzer. Same rules as ast_analysis.py, ported to JS:
real branching logic + no reference in any test file = a gap. Uses esprima
(a pure-Python JS parser) so this needs no Node.js install on your machine.

HONEST SCOPE LIMIT: this parses JavaScript syntax, not TypeScript type
annotations. A .ts/.tsx file using real TS syntax (interfaces, generics,
type annotations on params) will fail to parse and be silently skipped —
same as a Python file with a syntax error is skipped in ast_analysis.py.
Plain .js/.jsx files, and .ts files that happen to avoid TS-only syntax,
work fully. Closing this gap for real would mean a proper TS parser
(e.g. shelling out to the TypeScript compiler via Node.js) — a bigger
change, not done here.
"""
from __future__ import annotations

from pathlib import Path

import esprima

from .ast_analysis import FunctionCandidate, Gap, _score_gap

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "coverage", ".next",
    "vendor", "__pycache__",
}
TEST_FILE_MARKERS = (".test.", ".spec.", "__tests__")
BRANCH_TYPES = {
    "IfStatement", "ForStatement", "ForInStatement", "ForOfStatement",
    "WhileStatement", "DoWhileStatement", "TryStatement", "SwitchCase",
    "LogicalExpression",
}
JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")


def find_js_files(repo_path: Path) -> list[Path]:
    files = []
    for ext in JS_EXTENSIONS:
        for path in repo_path.rglob(f"*{ext}"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


def is_test_file(path: Path) -> bool:
    name = path.name.lower()
    return any(marker in name for marker in TEST_FILE_MARKERS) or "__tests__" in path.parts


def try_parse(source: str):
    """Tries module syntax first (import/export), falls back to script.
    Returns None on any parse failure — including real TS syntax esprima
    can't handle — same 'skip, don't crash' behavior as a Python SyntaxError."""
    for parser in (esprima.parseModule, esprima.parseScript):
        try:
            return parser(source, options={"loc": True}, tolerant=True)
        except Exception:
            continue
    return None


def walk_all(node):
    """Flat generator over every node in a subtree, regardless of shape."""
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            yield from walk_all(item)
        return
    if not hasattr(node, "type"):
        return
    yield node
    for key, value in vars(node).items():
        if key in ("loc", "range"):
            continue
        yield from walk_all(value)


def complexity_of(node) -> int:
    return sum(1 for n in walk_all(node) if n.type in BRANCH_TYPES)


def is_trivial_body(func_node) -> bool:
    if getattr(func_node, "expression", False):
        # Arrow function with an expression body, e.g. `x => x + 1` —
        # can't contain statement-level branches, only expressions.
        return complexity_of(func_node.body) == 0

    stmts = getattr(func_node.body, "body", [])
    if not stmts:
        return True
    if len(stmts) == 1 and stmts[0].type == "ReturnStatement":
        arg = getattr(stmts[0], "argument", None)
        if arg is None:
            return True
        return getattr(arg, "type", None) == "Literal"
    return False


def collect_test_identifiers(test_sources: list[str]) -> set[str]:
    names: set[str] = set()
    for source in test_sources:
        tree = try_parse(source)
        if tree is None:
            continue
        for node in walk_all(tree):
            if node.type == "Identifier":
                names.add(node.name)
            elif node.type == "Property":
                key = getattr(node, "key", None)
                if key is not None and getattr(key, "type", None) == "Identifier":
                    names.add(key.name)
    return names


def _collect_functions(node, class_stack: list[str], results: list[FunctionCandidate], file_rel: str):
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _collect_functions(item, class_stack, results, file_rel)
        return
    if not hasattr(node, "type"):
        return

    t = node.type
    pushed = False

    if t == "FunctionDeclaration" and node.id and not node.id.name.startswith("_"):
        _register(node, node.id.name, class_stack, results, file_rel)
    elif t == "VariableDeclarator":
        init = getattr(node, "init", None)
        idnode = getattr(node, "id", None)
        if (init is not None and getattr(init, "type", None) in ("FunctionExpression", "ArrowFunctionExpression")
                and idnode is not None and getattr(idnode, "type", None) == "Identifier"
                and not idnode.name.startswith("_")):
            _register(init, idnode.name, class_stack, results, file_rel)
    elif t == "MethodDefinition":
        key = getattr(node, "key", None)
        name = getattr(key, "name", None) if key is not None else None
        value = getattr(node, "value", None)
        if (name and not name.startswith("_") and name != "constructor"
                and value is not None and getattr(value, "type", None) == "FunctionExpression"):
            _register(value, name, class_stack, results, file_rel)
    elif t == "ClassDeclaration":
        cname = getattr(getattr(node, "id", None), "name", None)
        if cname:
            class_stack.append(cname)
            pushed = True

    for key, value in vars(node).items():
        if key in ("loc", "range"):
            continue
        _collect_functions(value, class_stack, results, file_rel)

    if pushed:
        class_stack.pop()


def _register(func_node, name: str, class_stack: list[str], results: list[FunctionCandidate], file_rel: str):
    cx = complexity_of(func_node.body)
    trivial = is_trivial_body(func_node)
    qualname = ".".join(class_stack + [name])
    results.append(FunctionCandidate(
        name=name, qualname=qualname, file=file_rel,
        start_line=func_node.loc.start.line, end_line=func_node.loc.end.line,
        complexity=cx, is_trivial=trivial,
    ))


def extract_functions(file_path: Path, repo_root: Path) -> list[FunctionCandidate]:
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    tree = try_parse(source)
    if tree is None:
        return []
    results: list[FunctionCandidate] = []
    rel_path = str(file_path.relative_to(repo_root))
    _collect_functions(tree, [], results, rel_path)
    return results


def find_gaps(repo_path: Path, *, min_complexity: int = 2) -> list[Gap]:
    all_files = find_js_files(repo_path)
    test_files = [f for f in all_files if is_test_file(f)]
    source_files = [f for f in all_files if f not in test_files]

    test_sources = []
    for f in test_files:
        try:
            test_sources.append(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    tested_identifiers = collect_test_identifiers(test_sources)

    gaps: list[Gap] = []
    for file_path in source_files:
        for func in extract_functions(file_path, repo_path):
            if func.is_trivial:
                continue
            if func.complexity < min_complexity:
                continue
            if func.name in tested_identifiers:
                continue

            score = _score_gap(func)
            gaps.append(Gap(
                function=func,
                reason=(
                    f"'{func.qualname}' has real branching logic "
                    f"(complexity {func.complexity}) and no reference "
                    f"found in any test file"
                ),
                score=score,
            ))

    gaps.sort(key=lambda g: g.score, reverse=True)
    return gaps
