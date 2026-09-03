"""
Demonstrates the Fixer pipeline running end-to-end against a REAL gap in a
REAL repo (not a synthetic test fixture).

Since there's no live LLM key yet, this uses DryRunLLMClient with a genuine
test written by hand for the actual top-ranked gap in Text-to-SQL-Agent
(save_and_generate_sql). This proves the full pipeline works — clone, find
gap, apply proposed fix, sandbox, run real pytest, verify — using one real
example instead of talking about it abstractly.

Run: python check_fixer.py
"""
from scanner.cloner import shallow_clone
from scanner.ast_analysis import find_gaps
from scanner.fixer import DryRunLLMClient, FixResponse, run_fixer

REPO = "TarunSinghChauhan/Text-to-SQL-Agent"

print(f"Cloning {REPO}...")
cloned = shallow_clone(REPO)

try:
    print("Scanning for gaps...")
    gaps = find_gaps(cloned.path)
    top_gap = gaps[0]
    print(f"\nTop gap: {top_gap.function.qualname} in {top_gap.function.file}")
    print(f"Reason: {top_gap.reason}\n")

    # Read the real source so the "proposed fix" below is honest — unchanged
    # code, just a real test added. We're not claiming to have found a bug;
    # we're demonstrating the pipeline can verify a genuine "add test only" fix.
    from scanner.fixer.pipeline import extract_function_source
    real_source = extract_function_source(cloned.path, top_gap)

    print("=" * 60)
    print("SIMULATING what a real LLM response would look like:")
    print("(unchanged code + a real test — no fabricated bug claim)")
    print("=" * 60)

    response = FixResponse(
        fixed_code=real_source,  # unchanged — being honest, not claiming a fake bug fix
        test_code=(
            "def test_save_and_generate_sql_smoke():\n"
            "    # Minimal smoke test proving the function is at least importable\n"
            "    # and callable — a real test suite would exercise its branches.\n"
            "    assert callable(save_and_generate_sql)\n"
        ),
        explanation="no bug found, added test only",
    )

    print("\nRunning through the Fixer pipeline (sandbox + real pytest run)...\n")
    result = run_fixer(cloned.path, top_gap, DryRunLLMClient(response))

    print("=" * 60)
    print(f"RESULT: {result.status.upper()}")
    print(f"REASON: {result.reason}")
    print(f"TESTS PASSED: {result.tests_passed}")
    print("=" * 60)

finally:
    cloned.cleanup()
