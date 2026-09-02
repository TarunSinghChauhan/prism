"""
Walks a Python repo and finds candidate "gaps": real functions with no
apparent test coverage.

Deliberately conservative — the whole point of PRism is not manufacturing
fake work, so this errs toward under-reporting (skipping trivial or
ambiguous cases) rather than flooding the list with noise.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", "venv", ".venv", "env", "node_modules", "__pycache__",
    "build", "dist", ".tox", "migrations", "site-packages", "vendor",
}

TEST_FILE_MARKERS = ("test_", "_test", "tests", "conftest")

# Trivial-body node types that don't count as "real logic" on their own.
TRIVIAL_NODE_TYPES = (ast.Pass, ast.Expr)


@dataclass
class FunctionCandidate:
    name: str
    qualname: str
    file: str
    start_line: int
    end_line: int
    complexity: int
    is_trivial: bool


@dataclass
class Gap:
    function: FunctionCandidate
    reason: str
    score: float


def find_python_files(repo_path: Path) -> list[Path]:
    files = []
    for path in repo_path.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def is_test_file(path: Path) -> bool:
    name = path.name.lower()
    return any(marker in name for marker in TEST_FILE_MARKERS) or "tests" in {
        p.lower() for p in path.parts
    }


def _complexity_of(node: ast.AST) -> int:
    """Rough cyclomatic-ish complexity: count of branching/loop nodes."""
    branch_types = (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.With)
    count = 0
    for child in ast.walk(node):
        if isinstance(child, branch_types):
            count += 1
    return count


def _is_trivial_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = node.body
    # Drop a leading docstring before judging triviality.
    if body and isinstance(body[0], ast.Expr) and isinstance(
        getattr(body[0], "value", None), (ast.Constant,)
    ):
        body = body[1:]
    if not body:
        return True
    if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Return)):
        # single bare return/pass with no computation
        if isinstance(body[0], ast.Return) and body[0].value is not None:
            return not isinstance(body[0].value, ast.Constant)
        return True
    return False


def extract_functions(file_path: Path, repo_root: Path) -> list[FunctionCandidate]:
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    rel_path = str(file_path.relative_to(repo_root))
    candidates: list[FunctionCandidate] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = ".".join(self.stack + [node.name])
            if not node.name.startswith("_"):  # skip private/dunder helpers
                candidates.append(
                    FunctionCandidate(
                        name=node.name,
                        qualname=qualname,
                        file=rel_path,
                        start_line=node.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        complexity=_complexity_of(node),
                        is_trivial=_is_trivial_body(node),
                    )
                )
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self._visit_func(node)

        def visit_AsyncFunctionDef(self, node):
            self._visit_func(node)

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    Visitor().visit(tree)
    return candidates


def collect_test_identifiers(repo_path: Path, test_files: list[Path]) -> set[str]:
    """All names referenced anywhere in test files — cheap proxy for 'is this called by a test'."""
    identifiers: set[str] = set()
    for path in test_files:
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
    return identifiers


def find_gaps(repo_path: Path, *, min_complexity: int = 2) -> list[Gap]:
    all_files = find_python_files(repo_path)
    test_files = [f for f in all_files if is_test_file(f)]
    source_files = [f for f in all_files if f not in test_files]

    tested_identifiers = collect_test_identifiers(repo_path, test_files)

    gaps: list[Gap] = []
    for file_path in source_files:
        for func in extract_functions(file_path, repo_path):
            if func.is_trivial:
                continue
            if func.complexity < min_complexity:
                continue
            if func.name in tested_identifiers:
                continue  # a test file references this name somewhere — not a gap

            score = _score_gap(func)
            gaps.append(
                Gap(
                    function=func,
                    reason=(
                        f"'{func.qualname}' has real branching logic "
                        f"(complexity {func.complexity}) and no reference "
                        f"found in any test file"
                    ),
                    score=score,
                )
            )

    gaps.sort(key=lambda g: g.score, reverse=True)
    return gaps


def _score_gap(func: FunctionCandidate) -> float:
    """Higher score = more worth fixing. Rewards real complexity, not length."""
    length = max(func.end_line - func.start_line, 1)
    density = func.complexity / length  # logic-per-line, not just a big function
    return round(func.complexity * 2 + density * 10, 2)
