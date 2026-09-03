"""
Real git operations backing the Shipper: create a branch, apply a verified
fix + its test, commit, push. Kept as thin subprocess wrappers (no GitPython
dependency) so behavior matches exactly what you'd see running git by hand.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..ast_analysis import Gap
from ..fixer.pipeline import FixResult


class GitError(Exception):
    pass


def _run(args: list[str], cwd: Path, timeout: int = 20) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(f"`{' '.join(args)}` failed: {proc.stderr.strip()}")
    return proc


def branch_name_for(gap: Gap) -> str:
    safe = gap.function.name.replace(".", "-")
    return f"prism/fix-{safe}"


def create_branch(repo_path: Path, branch: str) -> None:
    _run(["git", "checkout", "-b", branch], cwd=repo_path)


def apply_fix(repo_path: Path, gap: Gap, result: FixResult) -> None:
    """Writes the verified fix into the real file and adds the generated
    test as a new file — mirrors exactly what apply_and_run did in the
    sandbox, just against the real working copy this time."""
    if result.status != "verified":
        raise GitError(f"refusing to ship a '{result.status}' fix: {result.reason}")

    target_file = repo_path / gap.function.file
    lines = target_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    new_lines = (
        lines[: gap.function.start_line - 1]
        + result.fixed_code.splitlines()
        + lines[gap.function.end_line :]
    )
    target_file.write_text("\n".join(new_lines), encoding="utf-8")

    test_dir = repo_path / "tests"
    test_dir.mkdir(exist_ok=True)
    test_file = test_dir / f"test_prism_{gap.function.name}.py"
    module_path = gap.function.file[:-3].replace("/", ".").replace("\\", ".")
    test_file.write_text(
        f"from {module_path} import {gap.function.name}\n\n" + result.test_code,
        encoding="utf-8",
    )


def commit(repo_path: Path, message: str) -> None:
    _run(["git", "add", "-A"], cwd=repo_path)
    _run(["git", "config", "user.email", "prism-bot@local"], cwd=repo_path)
    _run(["git", "config", "user.name", "PRism"], cwd=repo_path)
    _run(["git", "commit", "-m", message], cwd=repo_path)


def push(repo_path: Path, branch: str, remote: str = "origin") -> None:
    _run(["git", "push", "-u", remote, branch], cwd=repo_path, timeout=30)
