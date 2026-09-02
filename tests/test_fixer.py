import textwrap
from pathlib import Path

from scanner.ast_analysis import find_gaps
from scanner.fixer import DryRunLLMClient, FixResponse, run_fixer

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


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pricing.py").write_text(textwrap.dedent(GOOD_FUNC))
    return repo


def get_gap(repo: Path):
    gaps = find_gaps(repo)
    assert len(gaps) == 1
    return gaps[0]


def test_genuine_fix_with_passing_test_is_verified(tmp_path: Path):
    repo = make_repo(tmp_path)
    gap = get_gap(repo)

    response = FixResponse(
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),  # unchanged — no real bug
        test_code=textwrap.dedent("""
            def test_member_discount():
                assert apply_discount(100, True, None) == 90.0

            def test_coupon_stacks_with_membership():
                assert apply_discount(100, True, "SAVE10") == 81.0
        """).strip(),
        explanation="no bug found, added test only",
    )
    result = run_fixer(repo, gap, DryRunLLMClient(response))
    assert result.status == "verified", result.reason
    assert result.tests_passed is True


def test_rejects_when_generated_test_actually_fails(tmp_path: Path):
    repo = make_repo(tmp_path)
    gap = get_gap(repo)

    response = FixResponse(
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),
        test_code=textwrap.dedent("""
            def test_wrong_expectation():
                assert apply_discount(100, True, None) == 999  # deliberately wrong
        """).strip(),
        explanation="no bug found, added test only",
    )
    result = run_fixer(repo, gap, DryRunLLMClient(response))
    assert result.status == "rejected"
    assert "failed" in result.reason.lower()


def test_rejects_invalid_syntax_in_fixed_code(tmp_path: Path):
    repo = make_repo(tmp_path)
    gap = get_gap(repo)

    response = FixResponse(
        fixed_code="def apply_discount(price, is_member, coupon:\n    return price",  # broken syntax
        test_code="def test_x():\n    assert apply_discount(1, True, None)",
        explanation="fixed a bug",
    )
    result = run_fixer(repo, gap, DryRunLLMClient(response))
    assert result.status == "rejected"
    assert "not valid python" in result.reason.lower()


def test_rejects_test_that_never_references_the_function(tmp_path: Path):
    repo = make_repo(tmp_path)
    gap = get_gap(repo)

    response = FixResponse(
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),
        test_code="def test_unrelated():\n    assert 1 + 1 == 2",
        explanation="no bug found, added test only",
    )
    result = run_fixer(repo, gap, DryRunLLMClient(response))
    assert result.status == "rejected"
    assert "never references" in result.reason.lower()


def test_rejects_diff_message_mismatch_claims_no_bug_but_code_changed(tmp_path: Path):
    repo = make_repo(tmp_path)
    gap = get_gap(repo)

    changed_code = textwrap.dedent(GOOD_FUNC).strip().replace("0.9", "0.5")  # actually changed
    response = FixResponse(
        fixed_code=changed_code,
        test_code="def test_x():\n    assert apply_discount(100, True, None)",
        explanation="no bug found, added test only",  # but code WAS changed — lying
    )
    result = run_fixer(repo, gap, DryRunLLMClient(response))
    assert result.status == "rejected"
    assert "mismatch" in result.reason.lower()


def test_rejects_empty_diff_that_claims_a_fix(tmp_path: Path):
    repo = make_repo(tmp_path)
    gap = get_gap(repo)

    response = FixResponse(
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),  # unchanged
        test_code="def test_x():\n    assert apply_discount(100, True, None)",
        explanation="fixed the discount stacking bug",  # claims a fix but nothing changed
    )
    result = run_fixer(repo, gap, DryRunLLMClient(response))
    assert result.status == "rejected"
    assert "empty diff" in result.reason.lower()
