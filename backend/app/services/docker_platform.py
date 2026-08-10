"""Resolve Docker `--platform` for trial sandboxes (Apple Silicon / amd64 images)."""

from __future__ import annotations

import os
import platform as py_platform


def host_is_arm64() -> bool:
    return py_platform.machine().lower() in ("arm64", "aarch64")


def resolve_docker_platform(
    task_platform: str | None,
    *,
    override: str | None = None,
) -> str | None:
    """Pick docker --platform: CLI override > task.toml > env > arm64 default."""
    if override:
        return override.strip() or None
    if task_platform:
        return task_platform
    env_val = (os.getenv("TRACETENSOR_DOCKER_PLATFORM") or "").strip()
    if env_val.lower() in ("native", "host", "none"):
        return None
    if env_val:
        return env_val
    # Many eval images (SWE-Bench, Terminal-Bench builds) ship linux/amd64 only.
    if host_is_arm64():
        return "linux/amd64"
    return None
