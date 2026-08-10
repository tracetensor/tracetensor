"""Unit tests for docker platform resolution and infra retry policy."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.models.enums import TrialStatus  # noqa: E402
from app.services.docker_platform import resolve_docker_platform  # noqa: E402
from app.services.infra_retry import is_infra_retryable  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


@dataclass
class _Outcome:
    status: str
    error: str | None = None
    trajectory: dict | None = None


print("== docker_platform ==")
with patch.dict(os.environ, {"TRACETENSOR_DOCKER_PLATFORM": "native"}, clear=False):
    check(
        "native env disables auto platform",
        resolve_docker_platform(None) is None,
    )
with patch.dict(os.environ, {"TRACETENSOR_DOCKER_PLATFORM": "linux/amd64"}, clear=False):
    check(
        "env default wins when task unset",
        resolve_docker_platform(None) == "linux/amd64",
    )
check(
    "task.toml platform wins",
    resolve_docker_platform("linux/arm64", override=None) == "linux/arm64",
)
check(
    "CLI override wins",
    resolve_docker_platform("linux/arm64", override="linux/amd64") == "linux/amd64",
)
with patch("app.services.docker_platform.host_is_arm64", return_value=True):
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("TRACETENSOR_DOCKER_PLATFORM", None)
        check(
            "arm64 host defaults to linux/amd64",
            resolve_docker_platform(None) == "linux/amd64",
        )

print("\n== infra_retry ==")
check(
    "setup docker error is retryable",
    is_infra_retryable(
        _Outcome(TrialStatus.ERROR.value, error="Room setup failed: docker pull I/O error")
    ),
)
check(
    "completed trial is not retryable",
    not is_infra_retryable(_Outcome(TrialStatus.COMPLETED.value)),
)
check(
    "error after agent steps is not retryable",
    not is_infra_retryable(
        _Outcome(TrialStatus.ERROR.value, error="docker", trajectory={"steps": [{"command": "ls"}]})
    ),
)

print(f"\n== summary: {passed} passed, {failed} failed ==")
sys.exit(1 if failed else 0)
