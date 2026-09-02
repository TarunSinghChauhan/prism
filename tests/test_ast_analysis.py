import textwrap
from pathlib import Path

import pytest

from scanner.ast_analysis import find_gaps


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def test_finds_untested_function_with_real_branching(repo: Path):
    write(repo, "src/pricing.py", """
        def apply_discount(price, is_member, coupon):
            if is_member:
                price *= 0.9
            if coupon == "SAVE10":
                price *= 0.9
            elif coupon == "SAVE20":
                price *= 0.8
            return price
    """)
    gaps = find_gaps(repo)
    assert len(gaps) == 1
    assert gaps[0].function.qualname == "apply_discount"


def test_skips_trivial_function(repo: Path):
    write(repo, "src/util.py", """
        def get_version():
            return "1.0.0"
    """)
    gaps = find_gaps(repo)
    assert gaps == []


def test_skips_function_referenced_in_tests(repo: Path):
    write(repo, "src/pricing.py", """
        def apply_discount(price, is_member, coupon):
            if is_member:
                price *= 0.9
            if coupon == "SAVE10":
                price *= 0.9
            elif coupon == "SAVE20":
                price *= 0.8
            return price
    """)
    write(repo, "tests/test_pricing.py", """
        from src.pricing import apply_discount

        def test_member_discount():
            assert apply_discount(100, True, None) == 90
    """)
    gaps = find_gaps(repo)
    assert gaps == []


def test_skips_private_helpers(repo: Path):
    write(repo, "src/internal.py", """
        def _helper(x):
            if x > 0:
                return x
            else:
                return -x
    """)
    gaps = find_gaps(repo)
    assert gaps == []


def test_low_complexity_below_threshold_is_skipped(repo: Path):
    write(repo, "src/simple.py", """
        def add_one(x):
            if x is None:
                return 0
            return x + 1
    """)
    gaps = find_gaps(repo, min_complexity=2)
    assert gaps == []
    gaps = find_gaps(repo, min_complexity=1)
    assert len(gaps) == 1


def test_ranks_more_complex_untested_gap_higher(repo: Path):
    write(repo, "src/a.py", """
        def simple_branch(x):
            if x:
                return 1
            return 0
    """)
    write(repo, "src/b.py", """
        def complex_branch(x, y, z):
            if x:
                if y:
                    for i in range(z):
                        if i % 2 == 0:
                            pass
                elif z:
                    try:
                        return z / y
                    except ZeroDivisionError:
                        return 0
            return None
    """)
    gaps = find_gaps(repo, min_complexity=1)
    assert gaps[0].function.qualname == "complex_branch"


def test_ignores_vendored_and_venv_dirs(repo: Path):
    write(repo, "vendor/thirdparty.py", """
        def messy_vendor_code(x):
            if x:
                for i in range(x):
                    if i:
                        pass
            return x
    """)
    gaps = find_gaps(repo, min_complexity=1)
    assert gaps == []
