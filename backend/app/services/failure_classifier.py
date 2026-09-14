"""What kind of failure was that, and is re-running it worth anything?

The retry decision used to be a flat tuple of substrings: if the error mentioned
"docker" or "container" anywhere, the trial was re-run. That gets two things
wrong. It retries failures that will never succeed — a bad registry credential
says "docker" and fails identically three times — and it classifies by accident:
a network-switch failure was retryable only because its message happened to
contain the word "container".

So failures get a *kind*, and the retry policy is a property of the kind. Rules
are ordered and first-match-wins, with the permanent kinds ahead of the transient
ones: "docker login: unauthorized" is an auth problem that mentions docker, not a
docker problem.

This still reads text, because that is what an agent CLI gives us — a subprocess
writes a message, not an exception type. What changed is that the text maps to a
named kind with a documented policy, instead of straight to a boolean.
"""

from __future__ import annotations

import re
from enum import Enum


class FailureKind(str, Enum):
    AUTH = "auth"
    """Credentials missing, invalid, or refused. Identical on every attempt."""

    CONFIG = "config"
    """The task asked for something this backend cannot do. Permanent."""

    CONTEXT_WINDOW = "context_window"
    """The model ran out of context. A property of the task, not the run."""

    RATE_LIMIT = "rate_limit"
    """Provider throttling or overload. Transient."""

    TIMEOUT = "timeout"
    """A deadline elapsed. Transient when nothing had started yet."""

    INFRA = "infra"
    """Sandbox, image or provider plumbing failed. Transient."""

    AGENT = "agent"
    """The agent ran and did not succeed. Re-running is a fresh paid attempt,
    not a recovery — that is a sampling decision the caller makes, not a retry."""

    UNKNOWN = "unknown"
    """Unrecognised. Treated as permanent: retrying a failure we cannot name is
    how a broken run turns into three broken runs."""


# Ordered. The first pattern that matches wins, so anything permanent has to be
# listed above the transient patterns whose vocabulary it shares.
_RULES: tuple[tuple[FailureKind, re.Pattern[str]], ...] = (
    (
        FailureKind.AUTH,
        re.compile(
            r"unauthorized|forbidden|authentication (failed|error)|invalid api[- ]?key"
            r"|\bapi[- ]?key (is )?(missing|invalid|not set)|\b(401|403)\b"
            r"|needs? [A-Z_]*API_KEY|credentials",
            re.I,
        ),
    ),
    (
        FailureKind.CONFIG,
        re.compile(
            r"not supported|unsupported|not implemented|cannot be overridden"
            r"|is not runnable|requires an egress-control sidecar|no such (agent|backend)"
            r"|unknown (agent|backend|network mode)",
            re.I,
        ),
    ),
    (
        FailureKind.CONTEXT_WINDOW,
        re.compile(
            r"context (window|length).{0,20}(exceed|too long|too large)|maximum context",
            re.I,
        ),
    ),
    (
        FailureKind.RATE_LIMIT,
        re.compile(r"rate[- ]?limit|too many requests|\b429\b|quota exceeded|overloaded", re.I),
    ),
    (
        FailureKind.TIMEOUT,
        re.compile(r"\btimed out\b|\btimeout\b|deadline exceeded", re.I),
    ),
    (
        FailureKind.INFRA,
        re.compile(
            r"room setup failed|docker|podman|container|sandbox|manifest|platform"
            r"|connection (reset|refused|closed)|i/o (timeout|error)|build failed"
            r"|no space left|temporary failure|502|503|bad gateway|service unavailable",
            re.I,
        ),
    ),
)

#: Kinds where the same run, tried again, has a real chance of succeeding.
#: Deliberately excludes AGENT: re-running a failed agent is another paid
#: attempt at the task, which is sampling, not error recovery.
_RETRYABLE = frozenset({FailureKind.INFRA, FailureKind.RATE_LIMIT, FailureKind.TIMEOUT})


def classify(error: str | None) -> FailureKind:
    """The kind of failure `error` describes."""
    if not error or not error.strip():
        return FailureKind.UNKNOWN
    for kind, pattern in _RULES:
        if pattern.search(error):
            return kind
    return FailureKind.UNKNOWN


def is_retryable_kind(kind: FailureKind) -> bool:
    return kind in _RETRYABLE
