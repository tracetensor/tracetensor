"""
The live-progress event contract — what the SSE streams actually send.

These payloads are the interface between the worker and the dashboard, and they
were the last untyped one in the system. Every event was a bare `dict` built at
the emit site and read by a `switch` in `frontend/index.html`; rename a key on
one side and the UI silently stops rendering that piece, with no error and no
failing test. `tests/test_frontend_contract.py` can check the URLs a client
calls, but it had no way to check the *shape* of what streams back.

Typing them does three things: the emit sites get checked by mypy, the models
are published into the OpenAPI document so a generated client gets real types,
and the set of event names becomes enumerable instead of folklore.

These are documentation-and-checking models, not validators in the hot path —
`event_bus` still moves plain dicts, because an event crosses a thread boundary
and a process boundary and paying for construction on each one buys nothing. The
guarantee comes from mypy at the emit sites plus the round-trip test that asserts
every emitted `type` is declared here.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

from app.schemas.common import JobStatusLiteral, TrialStatusLiteral

# Every event name the backend can emit. The stream is a union over these, and
# `tests/test_frontend_contract.py` fails if app/ emits a name that isn't here.
EventType = Literal[
    "phase",
    "step",
    "thinking",
    "agent_text",
    "warning",
    "guardrail",
    "trial_started",
    "trial_done",
    "job_started",
    "job_done",
    "run_started",
    "task_progress",
    "run_done",
    "diagnose",
    "error",
]

#: A phase or a single command: running → done, or error.
PhaseStatusLiteral = Literal["running", "done", "error"]

#: The four phases of a trial, in order.
TrialPhaseLiteral = Literal["setup", "agent", "verify", "score"]


class PhaseEvent(BaseModel):
    """A trial phase changed state. Drives the checklist in the UI."""

    type: Literal["phase"] = "phase"
    phase: TrialPhaseLiteral
    status: PhaseStatusLiteral
    trial_num: Optional[int] = None
    detail: Optional[str] = None
    #: Set on the terminal `score` event.
    reward: Optional[float] = None
    passed: Optional[bool] = None
    agent_error: Optional[str] = None


class StepEvent(BaseModel):
    """One command the agent (or the verifier) ran.

    Emitted twice per command: `running` when it starts, `done` with the result.
    stdout/stderr are truncated at the emit site — this is a progress marker, not
    a log pipe.
    """

    type: Literal["step"] = "step"
    phase: Literal["agent", "verifier"]
    command: str
    status: PhaseStatusLiteral
    trial_num: Optional[int] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class ThinkingEvent(BaseModel):
    """The agent is waiting on the model. Nothing to show but the spinner."""

    type: Literal["thinking"] = "thinking"
    phase: Literal["agent"] = "agent"
    provider: str
    model: str
    trial_num: Optional[int] = None


class WarningEvent(BaseModel):
    """A non-fatal condition worth surfacing.

    These exist so a 0.0 that came from a misconfigured task doesn't look like a
    0.0 the agent earned — e.g. an isolated verifier with no artifacts declared.
    """

    type: Literal["warning"] = "warning"
    phase: str
    message: str
    trial_num: Optional[int] = None


class GuardrailEvent(BaseModel):
    """A command matched a risky pattern (credential access, sandbox escape).

    The sandbox is the real defense; this makes the behavior visible rather than
    silent. In flag mode the command still runs.
    """

    type: Literal["guardrail"] = "guardrail"
    phase: Literal["agent"] = "agent"
    category: str
    message: str
    command: str
    trial_num: Optional[int] = None


class TrialStartedEvent(BaseModel):
    type: Literal["trial_started"] = "trial_started"
    trial_num: int


class TrialDoneEvent(BaseModel):
    type: Literal["trial_done"] = "trial_done"
    trial_num: int
    status: TrialStatusLiteral
    reward: Optional[float] = None
    passed: Optional[bool] = None
    duration_s: Optional[float] = None
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class JobStartedEvent(BaseModel):
    type: Literal["job_started"] = "job_started"
    agent: str
    model: Optional[str] = None
    n_trials: int
    concurrency: Optional[int] = None
    backend: Optional[str] = None


class JobDoneEvent(BaseModel):
    type: Literal["job_done"] = "job_done"
    status: JobStatusLiteral
    trials_passed: Optional[int] = None
    n_trials: Optional[int] = None
    pass_rate: Optional[float] = None
    error: Optional[str] = None


class RunStartedEvent(BaseModel):
    """A dataset run was queued. Emitted by the API, not a worker — no single
    worker owns a run."""

    type: Literal["run_started"] = "run_started"
    agent: str
    model: Optional[str] = None
    n_trials: int
    concurrency: int
    task_count: int


class TaskProgressEvent(BaseModel):
    """One task of a dataset run finished. Fanned in from its child job."""

    type: Literal["task_progress"] = "task_progress"
    job_id: str
    task_name: Optional[str] = None
    trials_completed: int
    trials_passed: int
    n_trials: int
    status: JobStatusLiteral


class RunDoneEvent(BaseModel):
    type: Literal["run_done"] = "run_done"
    status: JobStatusLiteral


class DiagnoseEvent(BaseModel):
    """A trial's post-hoc failure analysis finished (docs/DIAGNOSE.md).

    Fires after `trial_done` — diagnosis is a separate pass and must never
    delay the trial result. `primary` is the top failure class, or null when
    the analysis found nothing."""

    type: Literal["diagnose"] = "diagnose"
    trial_num: int
    primary: Optional[str] = None
    occurrence_count: int = 0
    engine: str = "rules"


class StreamErrorEvent(BaseModel):
    """The stream itself failed — currently only the deadline. Distinct from a
    job that failed, which is a `job_done` with status `failed`."""

    type: Literal["error"] = "error"
    detail: str


#: Everything a stream can send. Discriminated on `type`, so a generated client
#: narrows to the right model in a switch.
StreamEvent = Union[
    PhaseEvent,
    StepEvent,
    ThinkingEvent,
    WarningEvent,
    GuardrailEvent,
    TrialStartedEvent,
    TrialDoneEvent,
    JobStartedEvent,
    JobDoneEvent,
    RunStartedEvent,
    TaskProgressEvent,
    RunDoneEvent,
    DiagnoseEvent,
    StreamErrorEvent,
]


class StreamEventEnvelope(BaseModel):
    """Wrapper that exists purely to give the union a name in OpenAPI.

    SSE responses aren't described by FastAPI's response_model machinery (the
    body is a text stream, not JSON), so the stream endpoints reference this to
    get the event models into `components.schemas` where a client generator can
    find them.
    """

    event: StreamEvent = Field(discriminator="type")


__all__ = [
    "DiagnoseEvent",
    "EventType",
    "GuardrailEvent",
    "JobDoneEvent",
    "JobStartedEvent",
    "PhaseEvent",
    "RunDoneEvent",
    "RunStartedEvent",
    "StepEvent",
    "StreamErrorEvent",
    "StreamEvent",
    "StreamEventEnvelope",
    "TaskProgressEvent",
    "ThinkingEvent",
    "TrialDoneEvent",
    "TrialStartedEvent",
    "WarningEvent",
]
