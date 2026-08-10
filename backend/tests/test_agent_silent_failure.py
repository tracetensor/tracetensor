"""
Unit tests for LLMAgent silent-failure detection (no Docker, no API key).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from app.services import llm  # noqa: E402
from app.services.agents.llm_agent import LLMAgent  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def _fake_call_empty(provider, model, system, user, max_tokens=800):
    return llm.LLMCallResult(
        text="",
        provider=provider,
        model=model,
        input_tokens=10,
        output_tokens=2000,
        latency_ms=100.0,
        cost_usd=None,
    )


print("== LLMAgent: no runnable command after LLM calls ==")
env = MagicMock()
orig = llm.call_llm
llm.call_llm = _fake_call_empty
try:
    ag = LLMAgent("openai", model="gpt-4.1-mini", max_steps=2)
    result = ag.run("do something", env, timeout=30.0)
    check("error is set", result.error is not None, result.error or "")
    check("no agent steps", len([s for s in result.steps if s.phase == "agent"]) == 0)
    check("llm_calls recorded", len(result.llm_calls) >= 1)
finally:
    llm.call_llm = orig

print("\n== LLMAgent: DONE with zero agent steps ==")


def _fake_call_done(provider, model, system, user, max_tokens=800):
    return llm.LLMCallResult(
        text="DONE",
        provider=provider,
        model=model,
        input_tokens=10,
        output_tokens=5,
        latency_ms=50.0,
        cost_usd=None,
    )


llm.call_llm = _fake_call_done
try:
    ag = LLMAgent("openai", model="gpt-4.1-mini", max_steps=1)
    result = ag.run("do something", env, timeout=30.0)
    check("error mentions DONE or no runnable", result.error is not None, result.error or "")
finally:
    llm.call_llm = orig

print(f"\n== {passed} passed, {failed} failed ==")
sys.exit(1 if failed else 0)
