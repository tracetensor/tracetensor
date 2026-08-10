"""Infra-only trial retries — setup/Docker flakes, never paid LLM re-runs."""

from __future__ import annotations

from app.models.enums import TrialStatus

_INFRA_MARKERS = (
    "room setup failed",
    "docker",
    "connection reset",
    "connection refused",
    "i/o timeout",
    "i/o error",
    "build failed",
    "manifest",
    "platform",
    "no space left",
    "temporary failure",
    "deadline exceeded",
    "container",
)


def is_infra_retryable(outcome: object) -> bool:
    """True when a trial died before meaningful agent work (safe to re-run)."""
    if getattr(outcome, "status", None) != TrialStatus.ERROR.value:
        return False
    err = (getattr(outcome, "error", None) or "").lower()
    if not any(m in err for m in _INFRA_MARKERS):
        return False
    traj = getattr(outcome, "trajectory", None) or {}
    steps = traj.get("steps") if isinstance(traj, dict) else None
    if steps:
        return False
    llm = traj.get("llm_calls") if isinstance(traj, dict) else None
    if llm:
        return False
    return True
