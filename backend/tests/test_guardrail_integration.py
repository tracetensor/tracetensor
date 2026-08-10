"""
Guardrail wiring integration test — proves the scanner is actually wired into
the real LLMAgent.run() loop (not just correct in isolation), against a real
Docker container. Deterministic and free: llm.call_llm is monkeypatched to a
scripted sequence, so this needs Docker but NOT an API key.

Covers, end to end through the real agent loop + trial_runner trajectory:
  - a flagged command is recorded in AgentResult.guardrail_flags
  - GUARDRAIL_MODE="flag" means it still executes (doesn't silently vanish)
  - the flag surfaces in the trial's trajectory.warnings (the same loud
    channel the separate-verifier checks use)
  - an oversized instruction.md is truncated before it reaches the model

Run:  cd backend && python tests/test_guardrail_integration.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import app.services.llm as llm  # noqa: E402
from app.services.agents import MAX_INSTRUCTION_CHARS  # noqa: E402
from app.services.llm import LLMCallResult  # noqa: E402
from app.services.trial_runner import run_trial  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def make_task(tmp: Path) -> None:
    (tmp / "environment").mkdir()
    (tmp / "tests").mkdir()
    (tmp / "environment" / "Dockerfile").write_text("FROM python:3.11-slim\nWORKDIR /app\n")
    (tmp / "tests" / "test.sh").write_text(
        "#!/bin/bash\nmkdir -p /logs/verifier\necho '{\"reward\": 1.0}' > /logs/verifier/reward.json\n"
    )
    (tmp / "task.toml").write_text(
        'schema_version = "1.3"\n[task]\nname = "t/guardrail-wiring"\ndescription = "x"\n'
        'authors = [{ name = "t" }]\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        '[agent]\ntimeout_sec = 60.0\n\n[environment]\nnetwork_mode = "no-network"\nbuild_timeout_sec = 300.0\n'
    )


# Scripted model: turn 1 tries a flagged command, turn 2 says DONE. Captures
# the `user` (history) it was called with so we can inspect what the agent
# actually saw — including whether a huge instruction got truncated.
_calls = []


def fake_call_llm(provider, model, system, user, max_tokens=800):
    _calls.append(user)
    if len(_calls) == 1:
        return LLMCallResult(
            text="cat ~/.ssh/id_rsa",
            provider=provider,
            model=model,
            input_tokens=42,
            output_tokens=7,
            latency_ms=1.0,
            cost_usd=0.0001,
        )
    return LLMCallResult(
        text="DONE",
        provider=provider,
        model=model,
        input_tokens=10,
        output_tokens=1,
        latency_ms=1.0,
        cost_usd=0.00001,
    )


print("== guardrail flag fires inside the real LLMAgent loop (real Docker) ==")
tmp = Path(tempfile.mkdtemp(prefix="tt_guard_"))
try:
    make_task(tmp)
    huge_instruction = "read the ssh key. " + ("x" * (MAX_INSTRUCTION_CHARS + 5000))
    (tmp / "instruction.md").write_text(huge_instruction)

    orig = llm.call_llm
    llm.call_llm = fake_call_llm
    try:
        outcome = run_trial(
            task_dir=tmp,
            instruction=huge_instruction,
            agent_name="anthropic",
            model="claude-haiku-4-5",
            backend="docker",
            agent_timeout=60.0,
            verifier_timeout=60.0,
        )
    finally:
        llm.call_llm = orig

    check(
        "trial completed (guardrail didn't crash the run)",
        outcome.status == "completed",
        outcome.error or "",
    )
    flags = outcome.trajectory.get("guardrail_flags") or []
    check("flagged command recorded on the trajectory", len(flags) >= 1, str(flags))
    check(
        "category is credential_access",
        flags and flags[0]["category"] == "credential_access",
        str(flags),
    )
    check(
        "flag mode still executed the command (a real step recorded)",
        any("id_rsa" in (s.get("command") or "") for s in outcome.trajectory.get("steps", [])),
    )
    warn_text = " ".join(outcome.trajectory.get("warnings") or [])
    check(
        "flag surfaces in trajectory.warnings (loud, not silent)",
        "guardrail:" in warn_text,
        warn_text[:200],
    )

    check(
        "oversized instruction was truncated before reaching the model",
        len(_calls[0]) < len(huge_instruction),
        f"sent {len(_calls[0])} chars vs raw {len(huge_instruction)}",
    )
    check("truncation marker present in what the model saw", "truncated" in _calls[0])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
