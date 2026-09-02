"""
Shallow-clones a public GitHub repo into a temp directory.

Guards built in from day one because this module is the one called by the
public "run a live scan on your own repo" demo — untrusted input, has to be
cheap and safe to run on anyone's repo.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class CloneError(Exception):
    pass


GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_SIZE_MB = 200


@dataclass
class ClonedRepo:
    owner: str
    repo: str
    path: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def parse_repo_ref(ref: str) -> tuple[str, str]:
    """Accepts 'owner/repo', a full github.com URL, or 'github.com/owner/repo'."""
    ref = ref.strip()
    match = GITHUB_URL_RE.match(ref)
    if not match:
        raise CloneError(f"'{ref}' doesn't look like a GitHub repo reference")
    return match.group("owner"), match.group("repo")


def shallow_clone(
    ref: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
) -> ClonedRepo:
    owner, repo = parse_repo_ref(ref)
    url = f"https://github.com/{owner}/{repo}.git"
    dest = Path(tempfile.mkdtemp(prefix="prism-scan-"))

    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", url, str(dest)],
            check=True,
            timeout=timeout_seconds,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise CloneError(f"clone of {owner}/{repo} timed out after {timeout_seconds}s") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise CloneError(f"could not clone {owner}/{repo}: {stderr.strip() or 'unknown git error'}") from exc

    size_mb = _dir_size_mb(dest)
    if size_mb > max_size_mb:
        shutil.rmtree(dest, ignore_errors=True)
        raise CloneError(
            f"{owner}/{repo} is {size_mb:.0f}MB, over the {max_size_mb}MB demo-scan limit"
        )

    return ClonedRepo(owner=owner, repo=repo, path=dest)


def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)
