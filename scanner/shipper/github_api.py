"""
Minimal GitHub REST API client for opening PRs. Uses stdlib urllib, no
PyGithub dependency. The HTTP call itself is injectable (`_post`/`_get`
params) specifically so tests can verify request construction and response
handling without making a real network call or needing a real token.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

API_BASE = "https://api.github.com"


class GitHubAPIError(Exception):
    pass


@dataclass
class PullRequest:
    number: int
    url: str
    draft: bool


def _default_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise GitHubAPIError(f"GET {url} failed: HTTP {exc.code}") from exc


def _default_post(url: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise GitHubAPIError(f"POST {url} failed: HTTP {exc.code} — {detail}") from exc


def check_write_access(
    owner: str, repo: str, token: str,
    _get: Callable[[str, str], dict] = _default_get,
) -> bool:
    """True if the token's owner has push access to this repo — determines
    whether PRism can auto-merge (owned repo) or must stop at draft PR
    (external repo, per the read-only-on-others'-repos design rule)."""
    data = _get(f"{API_BASE}/repos/{owner}/{repo}", token)
    return bool(data.get("permissions", {}).get("push", False))


def open_pull_request(
    owner: str, repo: str, *, base: str, head: str, title: str, body: str,
    token: str, draft: bool,
    _post: Callable[[str, str, dict], dict] = _default_post,
) -> PullRequest:
    data = _post(
        f"{API_BASE}/repos/{owner}/{repo}/pulls",
        token,
        {"title": title, "body": body, "base": base, "head": head, "draft": draft},
    )
    if "number" not in data:
        raise GitHubAPIError(f"unexpected response opening PR: {data}")
    return PullRequest(number=data["number"], url=data.get("html_url", ""), draft=draft)
