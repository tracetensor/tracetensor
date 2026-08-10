"""Helpers for surfacing trial failure reasons in CLI output."""

from __future__ import annotations


def failure_hint_from_result(result: dict) -> str | None:
    """Best-effort one-line reason a trial failed (for CLI / dataset summaries)."""
    if result.get("passed"):
        return None
    reward = result.get("reward")
    if reward is not None and reward < 1.0:
        return f"reward {float(reward):.3f} — verifier did not pass (need 1.0)"
    if result.get("failure_hint"):
        return str(result["failure_hint"])[:120]
    if result.get("error"):
        return str(result["error"])[:120]
    traj = result.get("trajectory") or {}
    if traj.get("agent_error"):
        return str(traj["agent_error"])[:120]
    for step in reversed(traj.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if step.get("phase") == "verifier" and step.get("stderr"):
            for line in reversed(step["stderr"].strip().splitlines()):
                line = line.strip()
                if line and not line.startswith("Traceback"):
                    return str(line)[:120]
    return None


def enrich_trial_result(result: dict) -> dict:
    """Attach failure_hint to a trial result dict when missing."""
    hint = failure_hint_from_result(result)
    if hint:
        result = {**result, "failure_hint": hint}
    return result
