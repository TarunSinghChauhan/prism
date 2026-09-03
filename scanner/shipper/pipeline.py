"""
Ties git_ops + github_api together, and is where the core design rule from
day one actually gets enforced in code: owned repos can go straight to a
real PR; anyone else's repo stops at a DRAFT PR requiring explicit human
confirmation before it's marked ready-for-review. This function is the
single place that decision gets made — nothing upstream of it should ever
bypass this check.
"""
from __future__ import annotations

from pathlib import Path

from ..ast_analysis import Gap
from ..fixer.pipeline import FixResult
from . import git_ops
from .github_api import PullRequest, _default_get, _default_post, check_write_access, open_pull_request


class ShipError(Exception):
    pass


def ship_fix(
    repo_path: Path,
    gap: Gap,
    fix_result: FixResult,
    *,
    owner: str,
    repo: str,
    token: str,
    base_branch: str = "main",
    _get=_default_get,
    _post=_default_post,
) -> PullRequest:
    if fix_result.status != "verified":
        raise ShipError(f"cannot ship a '{fix_result.status}' fix — only verified fixes may be shipped")

    has_write_access = check_write_access(owner, repo, token, _get=_get)
    branch = git_ops.branch_name_for(gap)

    git_ops.create_branch(repo_path, branch)
    git_ops.apply_fix(repo_path, gap, fix_result)
    git_ops.commit(
        repo_path,
        message=f"Add test coverage for {gap.function.qualname}\n\n"
                f"{fix_result.reason}\n\nGenerated and verified by PRism.",
    )
    git_ops.push(repo_path, branch)

    # The enforcement point: external repos NEVER get a ready PR from this
    # path, regardless of what the fix verification said. Draft only.
    is_draft = not has_write_access

    pr = open_pull_request(
        owner, repo,
        base=base_branch, head=branch,
        title=f"Add test coverage: {gap.function.qualname}",
        body=(
            f"**Gap found:** {gap.reason}\n\n"
            f"**Fix status:** {fix_result.reason}\n\n"
            f"Opened automatically by PRism — verified diff-to-message match, "
            f"real test coverage, tests actually run and passed before this PR was opened."
            + ("\n\n_This is a draft: PRism doesn't have write access to this repo, "
               "so this PR requires human review before it's marked ready._" if is_draft else "")
        ),
        token=token,
        draft=is_draft,
        _post=_post,
    )
    return pr
