"""
prism-scanner: scan a public GitHub repo for genuine, untested logic gaps.

Usage:
    python -m scanner.cli owner/repo
    python -m scanner.cli owner/repo --demo        # read-only, ignores dedupe store
    python -m scanner.cli owner/repo --max-results 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ast_analysis import find_gaps
from .cloner import CloneError, shallow_clone
from .dedupe import SeenGapsStore

DEFAULT_STORE_PATH = Path.home() / ".prism" / "seen_gaps.json"


def run(ref: str, *, demo: bool, max_results: int, min_complexity: int) -> dict:
    cloned = shallow_clone(ref)
    try:
        gaps = find_gaps(cloned.path, min_complexity=min_complexity)

        if not demo:
            store = SeenGapsStore(DEFAULT_STORE_PATH)
            gaps = store.filter_new(cloned.owner, cloned.repo, gaps)

        top_gaps = gaps[:max_results]

        if not demo and top_gaps:
            store = SeenGapsStore(DEFAULT_STORE_PATH)
            store.mark_seen(cloned.owner, cloned.repo, top_gaps)

        return {
            "repo": f"{cloned.owner}/{cloned.repo}",
            "mode": "demo (read-only)" if demo else "full",
            "gap_count": len(top_gaps),
            "gaps": [
                {
                    "function": g.function.qualname,
                    "file": g.function.file,
                    "lines": f"{g.function.start_line}-{g.function.end_line}",
                    "complexity": g.function.complexity,
                    "score": g.score,
                    "reason": g.reason,
                }
                for g in top_gaps
            ],
        }
    finally:
        cloned.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a public repo for genuine untested-logic gaps.")
    parser.add_argument("repo", help="owner/repo or a github.com URL")
    parser.add_argument("--demo", action="store_true", help="read-only mode: don't write to the seen-gaps store")
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--min-complexity", type=int, default=2)
    args = parser.parse_args(argv)

    try:
        result = run(
            args.repo,
            demo=args.demo,
            max_results=args.max_results,
            min_complexity=args.min_complexity,
        )
    except CloneError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
