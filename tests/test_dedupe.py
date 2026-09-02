from pathlib import Path

from scanner.ast_analysis import find_gaps
from scanner.dedupe import SeenGapsStore

CODE = """
def apply_discount(price, is_member, coupon):
    if is_member:
        price *= 0.9
    if coupon == "SAVE10":
        price *= 0.9
    elif coupon == "SAVE20":
        price *= 0.8
    return price
"""


def test_second_scan_of_same_repo_finds_nothing_new(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pricing.py").write_text(CODE)

    store_path = tmp_path / "seen_gaps.json"
    store = SeenGapsStore(store_path)

    gaps = find_gaps(repo)
    assert len(gaps) == 1

    fresh = store.filter_new("acme", "widgets", gaps)
    assert len(fresh) == 1
    store.mark_seen("acme", "widgets", fresh)

    # Reload from disk to prove persistence, not just in-memory state.
    store2 = SeenGapsStore(store_path)
    fresh2 = store2.filter_new("acme", "widgets", find_gaps(repo))
    assert fresh2 == []


def test_same_gap_in_different_repo_is_not_deduped(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pricing.py").write_text(CODE)

    store_path = tmp_path / "seen_gaps.json"
    store = SeenGapsStore(store_path)

    gaps = find_gaps(repo)
    store.mark_seen("acme", "widgets", gaps)

    fresh = store.filter_new("other-org", "other-repo", find_gaps(repo))
    assert len(fresh) == 1
