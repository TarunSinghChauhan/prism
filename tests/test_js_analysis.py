import textwrap
from pathlib import Path

from scanner.js_analysis import find_gaps


def write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def test_finds_untested_function_declaration_with_real_branching(tmp_path: Path):
    write(tmp_path, "src/pricing.js", """
        function applyDiscount(price, isMember, coupon) {
            if (isMember) {
                price *= 0.9;
            }
            if (coupon === 'SAVE10') {
                price *= 0.9;
            } else if (coupon === 'SAVE20') {
                price *= 0.8;
            }
            return price;
        }
    """)
    gaps = find_gaps(tmp_path)
    assert len(gaps) == 1
    assert gaps[0].function.qualname == "applyDiscount"


def test_finds_untested_arrow_function_assigned_to_const(tmp_path: Path):
    write(tmp_path, "src/util.js", """
        const classify = (x) => {
            if (x > 100) {
                return 'high';
            } else if (x > 10) {
                return 'medium';
            }
            return 'low';
        };
    """)
    gaps = find_gaps(tmp_path)
    assert len(gaps) == 1
    assert gaps[0].function.qualname == "classify"


def test_finds_untested_class_method(tmp_path: Path):
    write(tmp_path, "src/service.js", """
        class OrderService {
            computeTotal(items, coupon) {
                let total = 0;
                for (const item of items) {
                    total += item.price;
                }
                if (coupon) {
                    total *= 0.9;
                }
                return total;
            }
        }
    """)
    gaps = find_gaps(tmp_path)
    assert len(gaps) == 1
    assert gaps[0].function.qualname == "OrderService.computeTotal"


def test_skips_trivial_function(tmp_path: Path):
    write(tmp_path, "src/version.js", """
        function getVersion() {
            return '1.0.0';
        }
    """)
    gaps = find_gaps(tmp_path)
    assert gaps == []


def test_skips_function_referenced_in_test_file(tmp_path: Path):
    write(tmp_path, "src/pricing.js", """
        function applyDiscount(price, isMember, coupon) {
            if (isMember) {
                price *= 0.9;
            }
            if (coupon === 'SAVE10') {
                price *= 0.9;
            }
            return price;
        }
    """)
    write(tmp_path, "src/pricing.test.js", """
        const { applyDiscount } = require('./pricing');

        test('member discount', () => {
            expect(applyDiscount(100, true, null)).toBe(90);
        });
    """)
    gaps = find_gaps(tmp_path)
    assert gaps == []


def test_skips_private_prefixed_functions(tmp_path: Path):
    write(tmp_path, "src/internal.js", """
        function _helper(x) {
            if (x > 0) {
                return x;
            } else {
                return -x;
            }
        }
    """)
    gaps = find_gaps(tmp_path)
    assert gaps == []


def test_low_complexity_below_threshold_is_skipped(tmp_path: Path):
    write(tmp_path, "src/simple.js", """
        function addOne(x) {
            if (x === null) {
                return 0;
            }
            return x + 1;
        }
    """)
    gaps = find_gaps(tmp_path, min_complexity=2)
    assert gaps == []
    gaps = find_gaps(tmp_path, min_complexity=1)
    assert len(gaps) == 1


def test_ignores_node_modules(tmp_path: Path):
    write(tmp_path, "node_modules/somelib/index.js", """
        function messyVendorCode(x) {
            if (x) {
                for (let i = 0; i < x; i++) {
                    if (i % 2 === 0) {}
                }
            }
            return x;
        }
    """)
    gaps = find_gaps(tmp_path, min_complexity=1)
    assert gaps == []


def test_typescript_file_with_real_ts_syntax_is_skipped_not_crashed(tmp_path: Path):
    """Honest scope check: a .ts file using actual TypeScript-only syntax
    (interfaces, typed params) can't be parsed by esprima and should be
    skipped silently — not raise an exception that breaks the whole scan."""
    write(tmp_path, "src/typed.ts", """
        interface User {
            id: number;
            name: string;
        }

        function getUser(id: number): User {
            if (id < 0) {
                throw new Error('bad id');
            }
            return { id, name: 'test' };
        }
    """)
    gaps = find_gaps(tmp_path)  # must not raise
    assert gaps == []  # TS syntax means esprima can't see this function at all


def test_plain_js_style_ts_file_still_works(tmp_path: Path):
    """A .ts file that happens to avoid TS-only syntax should still parse
    fine, since it's valid JS underneath."""
    write(tmp_path, "src/plain.ts", """
        function processOrder(order, coupon) {
            if (order.total > 100) {
                order.total *= 0.9;
            } else if (coupon) {
                order.total *= 0.95;
            }
            return order;
        }
    """)
    gaps = find_gaps(tmp_path)
    assert len(gaps) == 1
    assert gaps[0].function.qualname == "processOrder"
