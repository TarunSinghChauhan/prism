"""
The REAL test: runs the Fixer against a live Gemini call instead of
DryRunLLMClient. This is the one piece that couldn't be verified until now
— everything before this used a simulated LLM response.

Requires GEMINI_API_KEY to be set in your environment.

Run: python check_fixer_live.py
"""
import os

from scanner.cloner import shallow_clone
from scanner.ast_analysis import find_gaps
from scanner.fixer import GeminiClient, run_fixer

if not os.environ.get("GEMINI_API_KEY"):
    raise SystemExit("GEMINI_API_KEY is not set — set it before running this script.")

REPO = "TarunSinghChauhan/Text-to-SQL-Agent"

print(f"Cloning {REPO}...")
cloned = shallow_clone(REPO)

try:
    print("Scanning for gaps...")
    gaps = find_gaps(cloned.path)
    top_gap = gaps[0]
    print(f"\nTarget gap: {top_gap.function.qualname} in {top_gap.function.file}")
    print(f"Reason: {top_gap.reason}\n")

    print("Calling the LIVE Gemini API for a real proposed fix...")
    print("(this is the part that was never tested before)\n")

    result = run_fixer(cloned.path, top_gap, GeminiClient())

    print("=" * 60)
    print(f"RESULT: {result.status.upper()}")
    print(f"REASON: {result.reason}")
    print(f"TESTS PASSED: {result.tests_passed}")
    print("=" * 60)

    if result.status == "verified":
        print("\n--- Proposed fixed_code ---")
        print(result.fixed_code)
        print("\n--- Proposed test_code ---")
        print(result.test_code)

finally:
    cloned.cleanup()
