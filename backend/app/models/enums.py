"""
Status vocabularies — the state machines, in one place.

These were raw string literals scattered across models, services, routers, and
the CLI. A typo ("complete" for "completed") produced no error anywhere: the row
just never matched a query again, and the job looked stuck. As enums, the same
typo is an AttributeError at import.

Each is a `str` subclass, so every existing comparison, SQLAlchemy column
default, JSON serialization, and f-string keeps working unchanged — and the
stored values are identical to what's already in the database. Nothing here
requires a migration.

Use the members in code (`JobStatus.RUNNING`); the API contract is published to
clients through the `Literal[...]` aliases at the bottom.
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """A string enum whose repr in logs/errors is the value, not `Class.MEMBER`.

    Python 3.11 has StrEnum built in; this keeps 3.9/3.10 working, which the rest
    of app/ still targets (see the typing.Optional convention in the models).
    """

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class JobStatus(_StrEnum):
    """A job (one examination order) and a dataset run share this lifecycle:

    queued ──claim──> running ──> completed
                         └───────> failed
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> frozenset[str]:
        """States a worker will never move off. Used to decide when a run is
        finished and when an SSE stream can close."""
        return frozenset({cls.COMPLETED.value, cls.FAILED.value})


class TrialStatus(_StrEnum):
    """One attempt inside a job. Note this is NOT JobStatus: a trial that blew up
    is `error`, while a job that blew up is `failed`. Keeping the vocabularies
    separate is deliberate — a job with an errored trial is still `completed`,
    because a failing agent is a valid result, not a system failure."""

    SETUP = "setup"
    AGENT_RUNNING = "agent_running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    ERROR = "error"


class PhaseStatus(_StrEnum):
    """Progress markers on the live SSE stream — a phase or a single command
    starting, finishing, or blowing up.

    Deliberately its own vocabulary, not JobStatus. These describe what's
    happening *inside* a trial and are only ever read by the UI; nothing here is
    persisted, and "running" here means "this command is executing right now",
    not "this job has been claimed by a worker". Sharing the strings would invite
    someone to make them share a type.
    """

    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class TaskStatus(_StrEnum):
    """Registration lifecycle for an uploaded task.

    `registered` means stored but not runnable — it's missing something the
    validator requires. Only `ready_for_examination` can be examined.
    """

    REGISTERED = "registered"
    READY = "ready_for_examination"
