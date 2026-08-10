"""
What's inside a trial's `trajectory` — the JSONB blob nobody could see into.

`Trial.trajectory`, `llm_calls`, and `reward_payload` were typed `dict`. They're
the record of what an agent did and what it cost, they're what the export
endpoint hands to users, and they're read by the CLI, the vault rollups, and the
dashboard — all by string key, none of it checked.

These models are the written-down shape. They are NOT used to validate on the
write path: the trajectory is assembled across a thread boundary and stored
as-is, and re-validating every step would cost more than it catches. They exist
so the readers have something to type against, so the export format is
documented, and so a key rename shows up as a type error somewhere rather than
as a blank column in the UI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TrajectoryStep(BaseModel):
    """One command, as recorded. Mirrors `environment.ExecResult.to_step()`."""

    phase: str
    command: str
    exit_code: int
    #: Truncated at the source (8000 / 4000 chars) — a trajectory is a record,
    #: not a log archive.
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0


class LLMCall(BaseModel):
    """One model call's cost and latency.

    `cost_usd` is None when unknown rather than 0.0, and that distinction is
    load-bearing: a partial estimate must never be presentable as a complete
    one. `api_calls` is >1 for installed agents, which report a whole run as a
    single record carrying the true count.
    """

    provider: Optional[str] = None
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: Optional[float] = None
    cost_usd: Optional[float] = None
    api_calls: Optional[int] = None


class LLMUsageSummary(BaseModel):
    """The per-trial roll-up of `llm_calls`, so clients don't re-sum the list."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    #: None if ANY call's cost is unknown — see LLMCall.cost_usd.
    cost_usd: Optional[float] = None


class GuardrailFlag(BaseModel):
    """A command that matched a risky pattern. Recorded whether or not it ran."""

    category: str
    message: str
    command: str


class Trajectory(BaseModel):
    """The full record of one trial.

    Extra keys are tolerated on purpose: an agent may attach its own fields, and
    a trajectory written by an older version must still load.
    """

    model_config = {"extra": "allow"}

    agent: Optional[str] = None
    model: Optional[str] = None
    steps: List[TrajectoryStep] = Field(default_factory=list)
    agent_error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    llm_calls: List[LLMCall] = Field(default_factory=list)
    llm_usage_summary: Optional[LLMUsageSummary] = None
    #: Identifies the prompt/harness that produced this, so results recorded
    #: across versions stay comparable.
    prompt_version: Optional[str] = None
    guardrail_flags: List[GuardrailFlag] = Field(default_factory=list)
    #: An installed agent's own trajectory (e.g. Mini-SWE's), preserved verbatim.
    agent_native_trajectory: Optional[Dict[str, Any]] = None


class RewardPayload(BaseModel):
    """What the verifier reported beyond a single number.

    Free-form by design — a task's `tests/reward.toml` names its own criteria —
    so this documents the keys we produce without closing the set.
    """

    model_config = {"extra": "allow"}

    reward: Optional[float] = None
    criteria: Optional[Dict[str, Any]] = None
    judge_model: Optional[str] = None


__all__ = [
    "GuardrailFlag",
    "LLMCall",
    "LLMUsageSummary",
    "RewardPayload",
    "Trajectory",
    "TrajectoryStep",
]
