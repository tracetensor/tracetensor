"""Pre-run warnings for local Docker execution (heavy / unsupported tasks)."""

from __future__ import annotations

import re
import subprocess
from enum import Enum
from pathlib import Path

from app.schemas.task import TaskConfig
from app.services.docker_platform import host_is_arm64


class LocalRunTier(str, Enum):
    NATIVE = "native"
    HEAVY = "heavy"
    UNSUPPORTED = "unsupported"


_DOCKERFILE_FROM_RE = re.compile(r"^\s*FROM\s+(\S+)", re.MULTILINE | re.IGNORECASE)


def _docker_memory_mb() -> int | None:
    if not _which("docker"):
        return None
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        raw = (proc.stdout or "").strip()
        if not raw.isdigit():
            return None
        return int(raw) // (1024 * 1024)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def _task_has_compose(task_dir: Path) -> bool:
    env = task_dir / "environment"
    return (env / "docker-compose.yaml").exists() or (env / "docker-compose.yml").exists()


def _dockerfile_base_image(task_dir: Path) -> str | None:
    dockerfile = task_dir / "environment" / "Dockerfile"
    if not dockerfile.exists():
        return None
    m = _DOCKERFILE_FROM_RE.search(dockerfile.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def assess_local_run(task_dir: Path, cfg: TaskConfig) -> tuple[LocalRunTier, list[str]]:
    """Return run tier and human-readable warnings for local execution."""
    warnings: list[str] = []
    tier = LocalRunTier.NATIVE
    env = cfg.environment

    if _task_has_compose(task_dir):
        warnings.append(
            "environment/docker-compose.yaml found — TraceTensor runs a single container; "
            "compose stacks are not supported locally."
        )
        tier = LocalRunTier.UNSUPPORTED

    if host_is_arm64():
        platform = env.platform or "linux/amd64"
        if platform == "linux/amd64" or not env.platform:
            warnings.append(
                "Apple Silicon host — using linux/amd64 Docker emulation for many Hub images "
                "(slower than native arm64)."
            )
            if tier == LocalRunTier.NATIVE:
                tier = LocalRunTier.HEAVY

    docker_mem = _docker_memory_mb()
    if env.memory_mb and docker_mem and env.memory_mb > docker_mem:
        warnings.append(
            f"Task requests {env.memory_mb} MB RAM but Docker reports ~{docker_mem} MB — "
            "increase Docker Desktop memory or expect OOM failures."
        )
        tier = LocalRunTier.HEAVY

    image = env.docker_image or _dockerfile_base_image(task_dir)
    if image and any(k in image.lower() for k in ("snapshot", "swe-bench", "terminal-bench")):
        warnings.append(
            f"Prebuilt/heavy image {image!r} — first pull and run may take several minutes locally."
        )
        if tier == LocalRunTier.NATIVE:
            tier = LocalRunTier.HEAVY

    return tier, warnings
