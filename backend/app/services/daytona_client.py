"""Talking to Daytona: one shared client, and deciding when a failure is worth retrying.

Both concerns belong to every Daytona call and to none of the environment's own
logic, so they live here rather than inside `daytona_environment`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, TypeVar

log = logging.getLogger("tracetensor.daytona.client")

T = TypeVar("T")

# Clients are keyed by credentials, not by environment. A dataset run builds one
# environment per trial, and a client per environment means an HTTP connection
# pool per trial — dozens of pools opened and abandoned against the same host
# over a single job.
_client_lock = threading.Lock()
_clients: dict[tuple[str, str | None], object] = {}

#: Daytona's own exception classes that describe a *transient* condition. Named
#: rather than imported at module scope because the SDK is an optional
#: dependency. Everything absent from this list — authentication, validation,
#: not-found, conflict — fails identically on the next attempt, so retrying it
#: only delays a certain error and burns the caller's time budget.
_TRANSIENT_ERROR_NAMES = (
    "DaytonaConnectionError",
    "DaytonaConnectionTimeoutError",
    "DaytonaTimeoutError",
    "DaytonaInternalServerError",
    "DaytonaBadGatewayError",
    "DaytonaServiceUnavailableError",
    "DaytonaRateLimitError",
)


def get_client(api_key: str, api_url: str | None = None):
    """The shared client for these credentials, created on first use."""
    from daytona import Daytona, DaytonaConfig

    key = (api_key, api_url)
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            kwargs: dict = {"api_key": api_key}
            if api_url:
                kwargs["api_url"] = api_url
            client = Daytona(DaytonaConfig(**kwargs))
            _clients[key] = client
            log.debug("daytona_client_created api_url=%s", api_url or "default")
        return client


def reset_clients() -> None:
    """Drop cached clients. For tests — the cache is process-global."""
    with _client_lock:
        _clients.clear()


def transient_error_types() -> tuple[type, ...]:
    """The retryable exception classes, or () when the SDK isn't importable.

    Empty rather than `(Exception,)` on failure: a broken import should make us
    retry *nothing*, not retry everything including a bad API key.
    """
    try:
        import daytona
    except ImportError:
        return ()
    return tuple(
        cls
        for cls in (getattr(daytona, name, None) for name in _TRANSIENT_ERROR_NAMES)
        if isinstance(cls, type)
    )


def is_transient(exc: BaseException) -> bool:
    types = transient_error_types()
    return bool(types) and isinstance(exc, types)


def call_with_retry(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `fn`, retrying only transient Daytona failures with exponential backoff.

    A trial that dies on one flaky API call used to be re-run in full by the
    infra-retry path — paying for a whole sandbox, image and agent session to
    recover from a dropped connection. Retrying the single call is orders of
    magnitude cheaper.

    Note on creates: a transport failure can mean the request never landed, or
    that it landed and the response was lost. Retrying the latter can leave an
    orphan sandbox behind, which is why callers that create sandboxes also set
    an auto-delete interval — the orphan reaps itself rather than billing until
    someone notices.
    """
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            if not is_transient(exc) or attempt >= attempts:
                raise
            last = exc
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            log.warning(
                "daytona_retry op=%s attempt=%d/%d in=%.1fs error=%s",
                what,
                attempt,
                attempts,
                delay,
                f"{type(exc).__name__}: {exc}"[:200],
            )
            sleep(delay)
    raise last  # unreachable: the loop either returns or raises
