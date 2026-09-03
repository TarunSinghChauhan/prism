import subprocess
import textwrap
from pathlib import Path

from scanner.ast_analysis import find_gaps
from scanner.fixer.pipeline import FixResult
from scanner.shipper import git_ops

GOOD_FUNC = """
def apply_discount(price, is_member, coupon):
    if is_member:
        price *= 0.9
    if coupon == "SAVE10":
        price *= 0.9
    elif coupon == "SAVE20":
        price *= 0.8
    return price
"""


def make_repo_with_remote(tmp_path: Path) -> Path:
    """A real local git repo pushing to a real local bare 'remote' —
    proves push actually works without touching the network."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pricing.py").write_text(textwrap.dedent(GOOD_FUNC))
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
    return repo


def test_branch_name_is_derived_from_function_name():
    class FakeFunc:
        name = "apply_discount"
    class FakeGap:
        function = FakeFunc()
    assert git_ops.branch_name_for(FakeGap()) == "prism/fix-apply_discount"


def test_apply_fix_writes_code_and_test_file(tmp_path: Path):
    repo = make_repo_with_remote(tmp_path)
    gap = find_gaps(repo)[0]

    result = FixResult(
        gap=gap, status="verified", reason="tests passed against the sandboxed fix",
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),
        test_code="def test_apply_discount_basic():\n    assert apply_discount(100, True, None) == 90.0",
        tests_passed=True,
    )
    git_ops.apply_fix(repo, gap, result)

    test_file = repo / "tests" / f"test_prism_{gap.function.name}.py"
    assert test_file.exists()
    assert "apply_discount" in test_file.read_text()
    assert "from pricing import apply_discount" in test_file.read_text()


def test_apply_fix_refuses_a_rejected_result(tmp_path: Path):
    repo = make_repo_with_remote(tmp_path)
    gap = find_gaps(repo)[0]

    result = FixResult(gap=gap, status="rejected", reason="generated test failed")
    try:
        git_ops.apply_fix(repo, gap, result)
        assert False, "should have raised"
    except git_ops.GitError as exc:
        assert "rejected" in str(exc)


def test_full_branch_commit_push_cycle_against_real_bare_remote(tmp_path: Path):
    repo = make_repo_with_remote(tmp_path)
    gap = find_gaps(repo)[0]

    result = FixResult(
        gap=gap, status="verified", reason="tests passed against the sandboxed fix",
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),
        test_code="def test_apply_discount_basic():\n    assert apply_discount(100, True, None) == 90.0",
        tests_passed=True,
    )
    branch = git_ops.branch_name_for(gap)
    git_ops.create_branch(repo, branch)
    git_ops.apply_fix(repo, gap, result)
    git_ops.commit(repo, "Add test coverage for apply_discount")
    git_ops.push(repo, branch)

    # Prove it actually reached the "remote" — clone it fresh and check.
    clone = tmp_path / "verify_clone"
    remote = repo / ".." / "remote.git"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    branches = subprocess.run(
        ["git", "branch", "-a"], cwd=clone, capture_output=True, text=True
    ).stdout
    assert branch in branches
