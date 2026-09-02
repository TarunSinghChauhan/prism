"""
Pluggable LLM client for the Fixer stage.

The Fixer's job (write a real fix + test for a gap) genuinely needs an LLM —
that part can't be faked or made deterministic. What CAN be tested without
a live API key is everything downstream of "got a proposed fix back":
sandboxed test execution, diff verification, rejection logic. So this module
defines the interface plus a DryRunLLMClient that returns a fixed, known
response — used in tests to prove the rest of the pipeline actually works.

To go live, set GEMINI_API_KEY or GROQ_API_KEY and use the corresponding
client below. Neither has been called against a real API from this
environment — wire in your key and test that call path yourself before
trusting it in the Shipper.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class FixResponse:
    fixed_code: str
    test_code: str
    explanation: str


class LLMClient(ABC):
    @abstractmethod
    def propose_fix(self, prompt: str) -> FixResponse:
        ...


class DryRunLLMClient(LLMClient):
    """Deterministic stand-in used in tests. Returns a pre-set response
    regardless of prompt, so the pipeline logic around it can be verified
    without any network call or API key."""

    def __init__(self, response: FixResponse):
        self._response = response

    def propose_fix(self, prompt: str) -> FixResponse:
        return self._response


class GeminiClient(LLMClient):
    """Real client for Google AI Studio's free tier. NOT exercised by any
    test in this repo — requires GEMINI_API_KEY and a live network call.
    Verify this path yourself before the Fixer drives real PRs."""

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.0-flash"):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self.model = model

    def propose_fix(self, prompt: str) -> FixResponse:
        import json
        import urllib.request

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return FixResponse(
            fixed_code=parsed["fixed_code"],
            test_code=parsed["test_code"],
            explanation=parsed.get("explanation", ""),
        )
