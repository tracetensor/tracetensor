"""
Oracle agent suite — deterministic, zero-cost coverage of the full trial
pipeline (agent -> sandbox -> verifier -> score) with NO API key and NO
network calls. This is the CI-safe counterpart to test_phase2_e2e.py: it
proves the harness itself works without spending on a real model, using each
task's own solution/solve.sh as the "agent."

Requires: a running Docker daemon and the python:3.11-slim image (auto-pulled
on first run). Does NOT require any *_API_KEY.

Run:  cd backend && python tests/test_oracle_agent.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from app.services.trial_runner import run_trial  # noqa: E402

PROJECT = HERE.parents[2]
EXAMPLES = PROJECT / "examples"

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def run_oracle(task_dir: Path):
    instruction = (task_dir / "instruction.md").read_text()
    return run_trial(
        task_dir=task_dir,
        instruction=instruction,
        agent_name="oracle",
        model=None,
        backend="docker",
        agent_timeout=60.0,
        verifier_timeout=60.0,
    )


print("== oracle agent: examples/sort-csv (zero API calls) ==")
outcome = run_oracle(EXAMPLES / "sort-csv")
check("status completed", outcome.status == "completed", outcome.error or "")
check(
    "reward == 1.0 (reference solution passes its own verifier)",
    outcome.reward == 1.0,
    str(outcome.reward),
)
check("passed == True", outcome.passed is True)
check(
    "no LLM calls recorded (genuinely zero-cost)",
    outcome.trajectory.get("llm_calls") == [],
    str(outcome.trajectory.get("llm_calls")),
)
check(
    "llm_usage_summary reports 0 calls",
    outcome.trajectory.get("llm_usage_summary", {}).get("calls") == 0,
)

print("\n== oracle agent: examples/log-analyzer (a second, more complex task) ==")
if (EXAMPLES / "log-analyzer").exists():
    outcome2 = run_oracle(EXAMPLES / "log-analyzer")
    check("status completed", outcome2.status == "completed", outcome2.error or "")
    check("reward == 1.0", outcome2.reward == 1.0, str(outcome2.reward))
else:
    print("  (skipped — examples/log-analyzer not present)")

print("\n== oracle agent: graceful error when a task has no solution/solve.sh ==")
tmp = Path(tempfile.mkdtemp(prefix="tt_oracle_"))
try:
    (tmp / "environment").mkdir()
    (tmp / "tests").mkdir()
    (tmp / "environment" / "Dockerfile").write_text("FROM python:3.11-slim\nWORKDIR /app\n")
    (tmp / "tests" / "test.sh").write_text(
        "#!/bin/bash\nmkdir -p /logs/verifier\necho '{\"reward\": 0.0}' > /logs/verifier/reward.json\n"
    )
    (tmp / "instruction.md").write_text("no solution provided for this one")
    (tmp / "task.toml").write_text(
        'schema_version = "1.3"\n[task]\nname = "t/no-solution"\ndescription = "x"\n'
        'authors = [{ name = "t" }]\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        '[agent]\ntimeout_sec = 60.0\n\n[environment]\nnetwork_mode = "no-network"\nbuild_timeout_sec = 300.0\n'
    )
    outcome3 = run_oracle(tmp)
    check("no crash — reports an error instead", outcome3.status == "completed")
    check(
        "agent_error names the missing solution",
        "solve.sh" in (outcome3.trajectory.get("agent_error") or ""),
        outcome3.trajectory.get("agent_error"),
    )
    check(
        "reward reflects the empty output (0.0, not a false pass)",
        outcome3.reward == 0.0,
        str(outcome3.reward),
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
