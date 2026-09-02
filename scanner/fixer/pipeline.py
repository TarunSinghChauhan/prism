"""
The actual enforcement layer: takes whatever the LLM proposes and verifies
it before calling it "fixed." This is the part that keeps the Fixer honest —
it rejects on any of:

- invalid Python syntax in the proposed fix
- a claimed "no bug, test only" response where the code actually changed
- a generated test that never references the function it claims to test
- a generated test that fails when actually run
- an empty or no-op diff
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..ast_analysis import Gap
from .llm_client import FixResponse, LLMClient
from .prompt import build_fix_prompt


@dataclass
class FixResult:
    gap: Gap
    status: str  # "verified" | "rejected"
    reason: str
    fixed_code: str | None = None
    test_code: str | None = None
    tests_passed: bool | None = None


def extract_function_source(repo_path: Path, gap: Gap) -> str:
    file_path = repo_path / gap.function.file
    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[gap.function.start_line - 1 : gap.function.end_line])


def _normalize(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.strip().splitlines())


def _validate_syntax(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _test_references_function(test_code: str, func_name: str) -> bool:
    try:
        tree = ast.parse(test_code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == func_name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == func_name:
            return True
    return False


def apply_and_run(
    repo_path: Path,
    gap: Gap,
    response: FixResponse,
) -> FixResult:
    """Applies the proposed fix in an isolated copy of the repo, adds the
    proposed test, and actually runs it. Returns a rejection if anything
    about the proposal doesn't hold up — never a silent pass."""

    func_name = gap.function.name

    if not _validate_syntax(response.fixed_code):
        return FixResult(gap, "rejected", "proposed fixed_code is not valid Python")

    if not response.test_code.strip() or not _validate_syntax(response.test_code):
        return FixResult(gap, "rejected", "proposed test_code is missing or not valid Python")

    if not _test_references_function(response.test_code, func_name):
        return FixResult(
            gap, "rejected",
            f"generated test never references '{func_name}' — diff doesn't match stated intent",
        )

    original = extract_function_source(repo_path, gap)
    code_changed = _normalize(original) != _normalize(response.fixed_code)
    claims_no_bug = "no bug" in response.explanation.lower()
    if claims_no_bug and code_changed:
        return FixResult(
            gap, "rejected",
            "explanation claims no bug found, but fixed_code differs from the original — diff/message mismatch",
        )
    if not claims_no_bug and not code_changed:
        return FixResult(
            gap, "rejected",
            "explanation claims a fix, but fixed_code is identical to the original — empty diff",
        )

    sandbox = Path(tempfile.mkdtemp(prefix="prism-fix-"))
    try:
        shutil.copytree(repo_path, sandbox, dirs_exist_ok=True)

        target_file = sandbox / gap.function.file
        lines = target_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        new_lines = (
            lines[: gap.function.start_line - 1]
            + response.fixed_code.splitlines()
            + lines[gap.function.end_line :]
        )
        target_file.write_text("\n".join(new_lines), encoding="utf-8")

        test_dir = sandbox / "tests"
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / f"test_prism_generated_{func_name}.py"
        module_path = gap.function.file[:-3].replace("/", ".").replace("\\", ".")
        test_file.write_text(
            f"import sys\nsys.path.insert(0, {str(sandbox)!r})\n"
            f"from {module_path} import {func_name}\n\n"
            + response.test_code,
            encoding="utf-8",
        )

        proc = subprocess.run(
            ["python", "-m", "pytest", str(test_file), "-v"],
            cwd=sandbox, capture_output=True, text=True, timeout=30,
        )
        tests_passed = proc.returncode == 0

        return FixResult(
            gap,
            status="verified" if tests_passed else "rejected",
            reason="tests passed against the sandboxed fix" if tests_passed
                   else f"generated test failed against the sandboxed fix:\n{proc.stdout[-800:]}",
            fixed_code=response.fixed_code,
            test_code=response.test_code,
            tests_passed=tests_passed,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def run_fixer(repo_path: Path, gap: Gap, client: LLMClient) -> FixResult:
    source = extract_function_source(repo_path, gap)
    prompt = build_fix_prompt(gap, source)
    response = client.propose_fix(prompt)
    return apply_and_run(repo_path, gap, response)
