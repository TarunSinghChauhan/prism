# PRism Scanner

Finds genuine, untested-logic gaps in a Python repo — the first stage of the
PRism pipeline (Scanner → Fixer → Shipper).

## What it does

- Shallow-clones a repo (with timeout + size guards, safe for the public demo mode)
- Walks all Python source files via `ast`, skipping tests/vendor/venv
- Flags functions with real branching logic (`if`/`for`/`while`/`try`) that
  have **no reference anywhere in a test file**
- Scores gaps by complexity density, not raw size — a small function with a
  lot of real logic outranks a long function that's mostly boilerplate
- Dedupes against a local store so repeat scans don't resurface the same gap

## Known limitation (be aware before wiring this into the Fixer/Shipper)

"Has a test" is currently a **name-match heuristic**: it checks whether the
function's name appears anywhere in a test file's AST. Cheap and fast, but
imperfect — it can miss real coverage that only happens indirectly, and can
occasionally treat a same-named-but-unrelated function as "covered." Good
enough to validate the pipeline; before this drives real PR creation, swap
in `coverage.py` line-coverage data or an import-graph check.

## Usage

```bash
pip install -r requirements.txt

# Full mode — writes to the dedupe store
python -m scanner.cli owner/repo

# Demo mode — read-only, used by the public "scan your repo" button
python -m scanner.cli owner/repo --demo

python -m scanner.cli owner/repo --max-results 5 --min-complexity 3
```

Output is JSON: repo, mode, gap count, and each gap's function, file, line
range, complexity, score, and reason.

## Tests

```bash
python -m pytest tests/ -v
```

9 tests covering: real-gap detection, trivial-function skipping, test-
reference skipping, private-helper skipping, complexity thresholds, ranking
order, and vendor-dir exclusion. All passing.

## Next up

- JS/TS support (ts-morph-based, same gap/score model)
- Wire into Fixer (writes the fix + test, runs the real suite before proposing)
- Wire into Shipper (branch → commit → push → `gh pr create`)
- GitHub Actions `workflow_dispatch` wrapper for the live demo button

## Fixer (new)

`scanner/fixer/` — takes one Gap from the Scanner and turns it into a
verified fix + test, or a rejection with a specific reason.

**What's genuinely tested (6 tests, `tests/test_fixer.py`):** the
verification/rejection logic — a proposed fix has to be valid Python, its
test has to actually reference the function, "no bug found" can't be paired
with code that actually changed, a claimed fix can't be an empty diff, and
the generated test has to *actually pass* when run in a sandboxed copy of
the repo. All of this is proven using `DryRunLLMClient`, a deterministic
stand-in — no network call, no API key needed to trust this part.

**What's NOT yet tested: the real LLM call.** `GeminiClient` in
`llm_client.py` is written against Google AI Studio's free tier but has
never been exercised against the live API from this environment — no key
was available. Before trusting this in a real pipeline:

1. Set `GEMINI_API_KEY` (free at aistudio.google.com)
2. Run `run_fixer(repo_path, gap, GeminiClient())` on a real gap
3. Confirm the rejection logic still holds against real (not stubbed) LLM
   output — a live model can and will produce malformed JSON, hallucinated
   fixes, or tests that don't compile, and the pipeline needs to catch all
   of that the same way it catches the deliberately-bad DryRun cases above.

## Next up

- Wire a real LLM key in and validate against live output (see above)
- JS/TS support
- Shipper (branch → commit → push → `gh pr create`)
- Rate limiting on the public /api/scan endpoint (see chat — flagged, not yet built)

## Shipper (new)

`scanner/shipper/` — takes a verified FixResult and actually opens a PR.

**The one rule that matters, enforced in code:** `ship_fix()` checks write
access via the GitHub API before doing anything else. If the token doesn't
have push access to the target repo, the PR is **forced to draft** —
regardless of how good the fix is. This is proven by
`test_ship_fix_forces_draft_on_external_repo_with_no_write_access` in
`tests/test_shipper_pipeline.py`, arguably the most important test in this
whole project: it's the code-level guarantee behind "PRism never spams
external maintainers," not just a design doc claim.

**What's genuinely tested (13 tests across 2 files):**
- Git operations (branch, apply fix, commit, push) against a **real local
  git repo + real bare "remote"** — proves push actually works, no network
  or GitHub account needed to verify this part
- GitHub API request construction and response handling, via injected
  fake HTTP calls — proves the draft-flag logic and error handling, again
  without hitting the real API

**What's NOT yet tested:** an actual PR opened against real GitHub. The
`_get`/`_post` injection points mean the only untested seam is the real
HTTP call itself. Before using this for real: set a `GITHUB_TOKEN` with
repo scope, and run `ship_fix()` against a real verified FixResult (from
`check_fixer_live.py`) targeting one of your own repos first — never an
external one, until you've watched it behave correctly on your own.

## Next up

- Real end-to-end run: Scanner → Fixer (live Gemini) → Shipper → real PR,
  on one of your own repos, with a real GitHub token
- CI-watch + auto-merge for owned repos once the PR is open and green
- Rate limiting on the public /api/scan endpoint (still flagged, not built)
- JS/TS support
