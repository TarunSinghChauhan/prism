import subprocess
import textwrap
from pathlib import Path

from scanner.ast_analysis import find_gaps
from scanner.fixer.pipeline import FixResult
from scanner.shipper.github_api import GitHubAPIError, check_write_access, open_pull_request
from scanner.shipper.pipeline import ship_fix

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


def test_check_write_access_true_when_push_permission_present():
    def fake_get(url, token):
        assert "acme/widgets" in url
        return {"permissions": {"push": True, "pull": True}}
    assert check_write_access("acme", "widgets", "fake-token", _get=fake_get) is True


def test_check_write_access_false_for_read_only():
    def fake_get(url, token):
        return {"permissions": {"push": False, "pull": True}}
    assert check_write_access("someone-else", "their-repo", "fake-token", _get=fake_get) is False


def test_open_pull_request_sends_draft_flag_correctly():
    captured = {}
    def fake_post(url, token, payload):
        captured.update(payload)
        return {"number": 42, "html_url": "https://github.com/acme/widgets/pull/42"}

    pr = open_pull_request(
        "acme", "widgets", base="main", head="prism/fix-x",
        title="t", body="b", token="fake", draft=True, _post=fake_post,
    )
    assert pr.number == 42
    assert pr.draft is True
    assert captured["draft"] is True


def test_open_pull_request_raises_on_unexpected_response():
    def fake_post(url, token, payload):
        return {"message": "Validation Failed"}  # no "number" key — malformed
    try:
        open_pull_request("a", "b", base="main", head="x", title="t", body="b",
                           token="fake", draft=False, _post=fake_post)
        assert False, "should have raised"
    except GitHubAPIError:
        pass


def _make_repo(tmp_path: Path) -> Path:
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


def _verified_fix(gap):
    return FixResult(
        gap=gap, status="verified", reason="tests passed against the sandboxed fix",
        fixed_code=textwrap.dedent(GOOD_FUNC).strip(),
        test_code="def test_apply_discount_basic():\n    assert apply_discount(100, True, None) == 90.0",
        tests_passed=True,
    )


def test_ship_fix_opens_ready_pr_when_owner_has_write_access(tmp_path: Path):
    repo = _make_repo(tmp_path)
    gap = find_gaps(repo)[0]

    def fake_get(url, token):
        return {"permissions": {"push": True}}
    captured = {}
    def fake_post(url, token, payload):
        captured.update(payload)
        return {"number": 1, "html_url": "https://github.com/tarun/owned-repo/pull/1"}

    pr = ship_fix(
        repo, gap, _verified_fix(gap),
        owner="tarun", repo="owned-repo", token="fake",
        _get=fake_get, _post=fake_post,
    )
    assert pr.draft is False
    assert captured["draft"] is False


def test_ship_fix_forces_draft_on_external_repo_with_no_write_access(tmp_path: Path):
    """This is the test that matters most: the core design rule — external
    repos NEVER get a straight-to-ready PR from this pipeline, no matter
    what the fix verification says."""
    repo = _make_repo(tmp_path)
    gap = find_gaps(repo)[0]

    def fake_get(url, token):
        return {"permissions": {"push": False}}  # no write access — external repo
    captured = {}
    def fake_post(url, token, payload):
        captured.update(payload)
        return {"number": 2, "html_url": "https://github.com/someone-else/their-repo/pull/2"}

    pr = ship_fix(
        repo, gap, _verified_fix(gap),
        owner="someone-else", repo="their-repo", token="fake",
        _get=fake_get, _post=fake_post,
    )
    assert pr.draft is True
    assert captured["draft"] is True


def test_ship_fix_refuses_to_ship_a_rejected_result(tmp_path: Path):
    repo = _make_repo(tmp_path)
    gap = find_gaps(repo)[0]
    rejected = FixResult(gap=gap, status="rejected", reason="generated test failed")

    from scanner.shipper.pipeline import ShipError
    try:
        ship_fix(repo, gap, rejected, owner="a", repo="b", token="fake")
        assert False, "should have raised"
    except ShipError:
        pass
