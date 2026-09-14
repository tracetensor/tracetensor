"""Infra-only trial retries — setup/provider flakes, never paid LLM re-runs."""

from __future__ import annotations

from app.models.enums import TrialStatus
from app.services.failure_classifier import FailureKind, classify, is_retryable_kind


def failure_kind(outcome: object) -> FailureKind:
    """The classified kind of an errored trial (UNKNOWN if it didn't error)."""
    if getattr(outcome, "status", None) != TrialStatus.ERROR.value:
        return FailureKind.UNKNOWN
    return classify(getattr(outcome, "error", None))


def is_infra_retryable(outcome: object) -> bool:
    """True when a trial died before meaningful agent work (safe to re-run).

    Two independent gates, and both must pass:

    1. The failure's *kind* must be one that a retry can actually fix. This
       replaced a substring scan that retried anything mentioning "docker",
       including permanent credential errors.
    2. Nothing was spent. Any recorded step or LLM call means the agent had
       started working, so a re-run is a second paid attempt rather than a
       recovery — regardless of how the trial eventually died.
    """
    if getattr(outcome, "status", None) != TrialStatus.ERROR.value:
        return False
    if not is_retryable_kind(classify(getattr(outcome, "error", None))):
        return False
    traj = getattr(outcome, "trajectory", None) or {}
    if not isinstance(traj, dict):
        return True
    if traj.get("steps"):
        return False
    if traj.get("llm_calls"):
        return False
    return True
