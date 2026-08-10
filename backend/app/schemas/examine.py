"""API schemas for the Examination Room (Stage 2).

Uses typing.Optional / typing.List (not `X | None`) so Pydantic can resolve the
annotations on Python 3.9, matching the rest of app/schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel

from app.schemas.common import JobStatusLiteral, TrialStatusLiteral


class ExamineRequest(BaseModel):
    # A provider: "anthropic" | "openai" | "openrouter" ("claude" aliases anthropic).
    agent: str = "anthropic"
    model: Optional[str] = None  # a model id from the catalog (llm.MODELS)
    n_trials: int = 1
    backend: str = "docker"  # docker only (kept for forward-compat)
    # How many trials to run at once (trial parallelism). None → a safe
    # per-machine default (min(n_trials, 4)). Bounded 1..16 at the endpoint.
    concurrency: Optional[int] = None


class TrialView(BaseModel):
    id: uuid.UUID
    trial_num: int
    status: TrialStatusLiteral
    reward: Optional[float]
    passed: Optional[bool]
    duration_s: Optional[float]
    error: Optional[str]
    verifier_log: Optional[str]
    reward_payload: Optional[dict]
    trajectory: dict


class JobView(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    task_name: Optional[str]
    agent: str
    model: Optional[str]
    n_trials: int
    status: JobStatusLiteral
    trials_completed: int
    trials_passed: int
    pass_rate: Optional[float]
    error: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]
    # What the agent was tested on: instruction + verifier + files (for the log).
    context: Optional[dict] = None
    trials: List[TrialView] = []


class JobSummary(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    dataset_run_id: Optional[uuid.UUID] = None  # set when part of a dataset run
    agent: str
    model: Optional[str] = None
    task_name: Optional[str] = None
    status: JobStatusLiteral
    n_trials: int
    trials_passed: int
    pass_rate: Optional[float]
    created_at: datetime
    # Vault thin usage rollup (from trial trajectories; None when unknown / unused).
    duration_s: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


class AgentStatusEntry(BaseModel):
    """One row of the agent catalog. `status` is deliberately honest:
    verified = passed a real in-container run here; gated = wired but awaiting a
    key to verify; unsupported = not ready, and rejected at request time."""

    id: str
    label: str
    status: Literal["verified", "gated", "unsupported"]
    mark: str
    note: str


class BackendEntry(BaseModel):
    id: str
    label: str
    available: bool


class ProvidersView(BaseModel):
    """What the UI needs to populate its agent/model pickers.

    Typed rather than a bare dict so a generated client gets real types here
    instead of `Any` — this endpoint drives every run the user can start.
    """

    agents: List[dict]  # provider -> model catalog; shape owned by services/llm.py
    installed_agents: List[AgentStatusEntry]
    backends: List[BackendEntry]
