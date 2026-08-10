"""
Shapes shared by every domain's schemas.

`Page` is the one that matters: list endpoints used to return a bare JSON array,
which meant a client could render page 3 but had no way to know there were 47 —
no total, so no pagination control, and no way to tell "the last page" from "the
server truncated me". An array also can't grow: adding a field later would be a
breaking change, where an object just gains a key.

The status Literals below publish the state machines into the OpenAPI document.
Client generators emit a real union type from them instead of `str`, so a
consumer switching on a status gets a compile error when a state is added rather
than a branch that silently never runs.
"""

from __future__ import annotations

from typing import Generic, List, Literal, Optional, TypeVar, cast

from pydantic import BaseModel, Field

from app.models.enums import JobStatus, TaskStatus, TrialStatus

T = TypeVar("T")


class ErrorResponse(BaseModel):
    """The shape of every non-2xx body.

    Declared on each endpoint so a client can branch on the failure instead of
    string-matching prose. `detail` is FastAPI's own key, so this describes what
    the API already returns rather than changing it — the value is that it's now
    in the OpenAPI document instead of being folklore.
    """

    detail: str = Field(..., description="Human-readable explanation of the failure.")
    #: Present only on a 500. Correlates the response with the server log line
    #: that has the traceback, so a user can quote it in a bug report without us
    #: leaking a stack trace to them.
    error_id: Optional[str] = Field(
        None, description="Correlation id for the server-side log entry (5xx only)."
    )


#: OpenAPI ref for the SSE event union. FastAPI can't derive a response_model for
#: a text/event-stream body, so the stream endpoints point at the envelope's
#: schema by name — which is what pulls every event model into
#: `components.schemas` where a client generator can reach it.
SSE_EVENT_REF = "#/components/schemas/StreamEventEnvelope"


#: Attach to a route's `responses=` so the error shape is published alongside the
#: success one. Every endpoint can 500; the rest are per-endpoint.
ERROR_RESPONSES: dict = {
    400: {"model": ErrorResponse, "description": "The request was malformed or unrunnable."},
    404: {"model": ErrorResponse, "description": "No such resource."},
    409: {"model": ErrorResponse, "description": "Conflicts with the current state."},
    413: {"model": ErrorResponse, "description": "Upload exceeded the size cap."},
    422: {"model": ErrorResponse, "description": "Request failed validation."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    500: {"model": ErrorResponse, "description": "Unexpected server error."},
}


class Page(BaseModel, Generic[T]):
    """One page of a list endpoint, with enough context to page through it."""

    items: List[T]
    #: Total matching rows, ignoring limit/offset — what a "page 3 of 47" control
    #: needs. Counted with a separate COUNT query, so it's exact, not an estimate.
    total: int = Field(..., description="Total rows matching the query, ignoring pagination.")
    limit: int
    offset: int

    @classmethod
    def of(cls, items: List[T], total: int, limit: int, offset: int) -> "Page[T]":
        return cls(items=items, total=total, limit=limit, offset=offset)


# Literal aliases mirroring app/models/enums.py. Kept in sync by
# tests/test_architecture.py, which fails if an enum gains a member that isn't
# published here — an undocumented status is a contract a client can't honor.
JobStatusLiteral = Literal["queued", "running", "completed", "failed"]
TrialStatusLiteral = Literal["setup", "agent_running", "verifying", "completed", "error"]
TaskStatusLiteral = Literal["registered", "ready_for_examination"]

_LITERALS = {
    JobStatus: JobStatusLiteral,
    TrialStatus: TrialStatusLiteral,
    TaskStatus: TaskStatusLiteral,
}


def as_job_status(value: str) -> JobStatusLiteral:
    """Narrow a status string read from the database to its published Literal.

    The ORM column is a plain VARCHAR, so every status arrives typed `str` and
    can't be handed to a Literal-typed response field without this. It's a cast,
    not a check: what actually guarantees the value is the enum on the write side
    (nothing in app/ writes a bare status literal — tests/test_architecture.py
    enforces that) plus the migration that created the column.

    Pydantic still validates on the way out, so a value that somehow escaped both
    guarantees fails loudly at the response boundary rather than reaching a client.
    """
    return cast(JobStatusLiteral, value)


def as_trial_status(value: str) -> TrialStatusLiteral:
    """See as_job_status."""
    return cast(TrialStatusLiteral, value)


def as_task_status(value: str) -> TaskStatusLiteral:
    """See as_job_status."""
    return cast(TaskStatusLiteral, value)


def literal_values(alias: object) -> set:
    """The strings a Literal alias admits — used by the contract test."""
    from typing import get_args

    return set(get_args(alias))


def as_optional_job_status(value: Optional[str]) -> Optional[JobStatusLiteral]:
    """See as_job_status. None passes through — an empty leaderboard cell."""
    return None if value is None else cast(JobStatusLiteral, value)


__all__ = [
    "ERROR_RESPONSES",
    "SSE_EVENT_REF",
    "ErrorResponse",
    "JobStatusLiteral",
    "as_job_status",
    "as_optional_job_status",
    "as_task_status",
    "as_trial_status",
    "Page",
    "TaskStatusLiteral",
    "TrialStatusLiteral",
    "literal_values",
]
