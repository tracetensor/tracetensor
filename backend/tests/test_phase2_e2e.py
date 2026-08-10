"""
Phase 2 end-to-end test — the Examination Room with a REAL LLM agent.

Runs the full trial loop against real Anthropic/OpenAI/OpenRouter APIs inside
a real Docker container. This is the end-to-end smoke test: an LLM agent
reads instruction.md, executes bash commands in an isolated container, and
the verifier scores the result.

Requires:
  - Docker daemon running
  - ANTHROPIC_API_KEY (or OPENAI_API_KEY / OPENROUTER_API_KEY) in backend/.env
  - python:3.11-slim image (auto-pulled on first run)

Run:  cd backend && venv/bin/python tests/test_phase2_e2e.py
Or:   cd backend && venv/bin/python tests/test_phase2_e2e.py --provider openai
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

# Load backend/.env so ANTHROPIC_API_KEY etc. are available.
from app.core.config import settings  # noqa: E402 (also loads .env)
from app.services import llm  # noqa: E402
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


def print_trajectory_summary(outcome, max_steps: int = 5) -> None:
    """Show the first few steps the agent took, for debugging."""
    steps = outcome.trajectory.get("steps", [])
    print(f"\n  Trajectory ({len(steps)} step{'s' if len(steps) != 1 else ''}):")
    for i, s in enumerate(steps[:max_steps]):
        cmd = (s["command"] or "").splitlines()[0][:80]
        exit_code = s["exit_code"]
        tag = "AGENT" if s["phase"] == "agent" else "VERIFY"
        print(f"    [{i + 1}] {tag} $ {cmd}  (exit={exit_code})")
    if len(steps) > max_steps:
        print(f"    … and {len(steps) - max_steps} more step(s)")


def pick_provider() -> tuple[str, str]:
    """Pick the first provider whose key is present. Prefer anthropic."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=list(llm.PROVIDERS), default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    if args.provider:
        provider = args.provider
    else:
        avail = settings.available_providers()
        for p in ("anthropic", "openai", "openrouter"):
            if avail.get(p):
                provider = p
                break
        else:
            print(
                "ERROR: no LLM provider key set in backend/.env "
                "(need ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY)."
            )
            sys.exit(2)

    model = args.model or llm.default_model(provider)
    return provider, model


def main() -> int:
    global passed, failed

    provider, model = pick_provider()
    print(f"\n== Phase 2 E2E — provider={provider}, model={model} ==")

    # Sanity: the provider factory should build our LLMAgent.
    from app.services.agents import make_agent
    from app.services.agents.llm_agent import LLMAgent

    agent = make_agent(provider, task_dir=EXAMPLES / "sort-csv", model=model)
    check("make_agent returns LLMAgent", isinstance(agent, LLMAgent))
    check("agent has correct provider", agent.provider == llm.canonical_provider(provider))
    check("agent has correct model", agent.model == model)

    # ==================================================================
    # Test 1: sort-csv — a small, well-scoped task the LLM should solve.
    # ==================================================================
    task = EXAMPLES / "sort-csv"
    instruction = (task / "instruction.md").read_text()
    print("\n-- Task: sort-csv (network=no-network) --")
    print(f"   instruction: {instruction.strip()[:100]}")

    t0 = time.time()
    outcome = run_trial(
        task_dir=task,
        instruction=instruction,
        agent_name=provider,
        model=model,
        backend="docker",
        agent_timeout=180.0,  # generous — real LLM latency + docker build
        verifier_timeout=60.0,
        pass_threshold=1.0,
    )
    elapsed = time.time() - t0
    print(
        f"\n  outcome: status={outcome.status} reward={outcome.reward} "
        f"passed={outcome.passed} dur={outcome.duration_s:.1f}s (wall={elapsed:.1f}s)"
    )
    if outcome.error:
        print(f"  error: {outcome.error[:200]}")
    print_trajectory_summary(outcome)

    check(
        "trial completed (no infra failure)",
        outcome.status == "completed",
        f"status={outcome.status}",
    )
    check(
        "reward is a number", isinstance(outcome.reward, (int, float)), f"reward={outcome.reward}"
    )
    check("reward in [0, 1]", outcome.reward is not None and 0.0 <= outcome.reward <= 1.0)
    check(
        "agent produced at least one step",
        any(s["phase"] == "agent" for s in outcome.trajectory.get("steps", [])),
    )
    check(
        "verifier ran", any(s["phase"] == "verifier" for s in outcome.trajectory.get("steps", []))
    )
    check("trajectory records agent name", outcome.trajectory.get("agent") == provider)
    check("trajectory records model", outcome.trajectory.get("model") == model)
    check("verifier log captured", bool(outcome.verifier_log))

    # A real LLM may or may not solve this on any given attempt. We don't fail
    # the suite on that — we just report it prominently. The important thing is
    # the plumbing works end-to-end.
    print(
        f"\n  === LLM verdict on sort-csv: "
        f"{'SOLVED (reward=1.0)' if outcome.passed else f'DID NOT PASS (reward={outcome.reward})'} ==="
    )

    # ==================================================================
    # Test 2: log-analyzer — a harder task with multi-criteria reward.
    # ==================================================================
    task = EXAMPLES / "log-analyzer"
    if task.exists():
        print("\n-- Task: log-analyzer (multi-criteria reward) --")
        instruction2 = (task / "instruction.md").read_text()
        t0 = time.time()
        outcome2 = run_trial(
            task_dir=task,
            instruction=instruction2,
            agent_name=provider,
            model=model,
            backend="docker",
            agent_timeout=240.0,
            verifier_timeout=90.0,
        )
        elapsed = time.time() - t0
        print(
            f"\n  outcome: status={outcome2.status} reward={outcome2.reward} "
            f"passed={outcome2.passed} dur={outcome2.duration_s:.1f}s (wall={elapsed:.1f}s)"
        )
        if outcome2.reward_payload:
            print(f"  reward_payload: {outcome2.reward_payload}")
        print_trajectory_summary(outcome2)

        check("log-analyzer trial completed", outcome2.status == "completed")
        check("log-analyzer reward parsed", outcome2.reward is not None)
        check("log-analyzer produced trajectory", len(outcome2.trajectory.get("steps", [])) > 0)
    else:
        print("\n-- log-analyzer example not present, skipping second task --")

    # ==================================================================
    # Summary
    # ==================================================================
    print(f"\n{'=' * 60}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 60}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
