"""Builds the prompt sent to the LLM for a given gap."""
from __future__ import annotations

from ..ast_analysis import Gap

FIX_PROMPT_TEMPLATE = """You are fixing a specific, narrow gap in a codebase — not refactoring, not improving style, just closing one real testing gap.

Function: {qualname}
File: {file}
Lines: {start}-{end}

Source of the function:
```python
{source}
```

Why this was flagged: {reason}

Your task:
1. If the function has a genuine bug, fix ONLY that bug — do not change unrelated behavior.
2. If the function is correct but untested, do not change its code at all — return it unchanged.
3. Write a real pytest test that actually exercises this function's branching logic (not a trivial smoke test).

Respond as JSON with exactly these keys:
{{
  "fixed_code": "<the function's full source, fixed or unchanged>",
  "test_code": "<a complete pytest test function>",
  "explanation": "<one sentence: what was wrong, or 'no bug found, added test only'>"
}}
"""


def build_fix_prompt(gap: Gap, source: str) -> str:
    return FIX_PROMPT_TEMPLATE.format(
        qualname=gap.function.qualname,
        file=gap.function.file,
        start=gap.function.start_line,
        end=gap.function.end_line,
        source=source,
        reason=gap.reason,
    )
