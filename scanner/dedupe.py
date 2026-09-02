"""
Tracks gaps already seen for a given repo so repeat scans (or repeat demo
runs against the same public repo) don't keep resurfacing the same finding.

Zero-cost by design: a flat JSON file, no database service required.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .ast_analysis import Gap


def gap_fingerprint(owner: str, repo: str, gap: Gap) -> str:
    """Stable id for a gap: same function, same file, same repo == same gap,
    even if line numbers shift slightly from unrelated edits elsewhere."""
    raw = f"{owner}/{repo}:{gap.function.file}:{gap.function.qualname}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SeenGapsStore:
    def __init__(self, store_path: Path):
        self.store_path = store_path
        self._seen: set[str] = set()
        if store_path.exists():
            data = json.loads(store_path.read_text())
            self._seen = set(data.get("seen", []))

    def filter_new(self, owner: str, repo: str, gaps: list[Gap]) -> list[Gap]:
        fresh = []
        for gap in gaps:
            fp = gap_fingerprint(owner, repo, gap)
            if fp not in self._seen:
                fresh.append(gap)
        return fresh

    def mark_seen(self, owner: str, repo: str, gaps: list[Gap]) -> None:
        for gap in gaps:
            self._seen.add(gap_fingerprint(owner, repo, gap))
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps({"seen": sorted(self._seen)}, indent=2))
