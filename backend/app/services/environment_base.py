"""
Backend-agnostic core of the execution environments: the ExecResult record,
the BaseEnvironment contract every backend implements, and the standard
container directory layout. Concrete backends (Docker/Podman in
app.services.environment, Daytona in app.services.daytona_environment) build
on these; everything here is importable without pulling in any runtime.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import-cycle-free typing: backend_capabilities imports this module
    from app.services.backend_capabilities import BackendCapabilities


@dataclass
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    # Free-form label for the trajectory (e.g. "agent", "verifier").
    phase: str = "agent"

    def to_step(self) -> dict:
        return {
            "phase": self.phase,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout[-8000:],  # cap to keep records sane
            "stderr": self.stderr[-4000:],
            "duration_s": round(self.duration_s, 3),
        }


class BaseEnvironment(abc.ABC):
    """Uniform room interface — the plug-in contract for an execution backend.

    A backend is constructed as `Backend(task_dir, **kwargs)` (kwargs are the
    task's environment config: docker_image, network_mode, cpus, workdir, …) and
    implements the full lifecycle:
        build → setup → exec/read_file/write_file/copy_in/set_network/
        transfer_from → teardown
    Register one with app.services.environment.register_environment(name, cls)
    and it's selectable everywhere via make_environment(backend=name, …).

    These are real abstract methods. They used to be empty bodies (`...`), which
    meant a backend that forgot `exec` returned None from every command: the
    agent appeared to run, produced no steps, and scored 0.0 — identical to an
    agent that genuinely failed. Now that mistake is a TypeError at construction,
    naming the missing method.
    """

    @abc.abstractmethod
    def __init__(self, task_dir: Path, **kwargs: object) -> None: ...

    @abc.abstractmethod
    def build(self) -> None:
        """Prepare the image. Idempotent — trials share a per-task image tag."""

    @abc.abstractmethod
    def setup(self) -> None:
        """Start the room and stage the task's files into it."""

    @abc.abstractmethod
    def exec(
        self,
        command: str,
        phase: str = "agent",
        timeout: float | None = None,
        as_user: str | None = None,
        env: dict | None = None,
    ) -> ExecResult:
        """Run one command inside the room and record what it did.

        Must return an ExecResult even when the command fails — a nonzero exit is
        ordinary data here, not an error to raise on.
        """

    @abc.abstractmethod
    def read_file(self, path: str) -> str | None:
        """Read a file from inside the room; None when it doesn't exist."""

    @abc.abstractmethod
    def write_file(self, path: str, content: bytes) -> None: ...

    @abc.abstractmethod
    def copy_in(self, src_dir: Path, dest: str) -> None: ...

    @abc.abstractmethod
    def set_network(self, mode: str | None, allowed_hosts: list[str] | None = None) -> None:
        """Apply a phase's network policy (public / no-network). Called between
        phases, so a task can grant the agent egress and deny it to the verifier.
        A backend that cannot enforce isolation must raise rather than silently
        run with more access than the task asked for."""

    @abc.abstractmethod
    def transfer_from(self, other: "BaseEnvironment", paths: list[str]) -> list[str]:
        """Copy paths from another room into this one; return the ones that were
        absent at the source so the caller can warn instead of scoring a silent 0."""

    @abc.abstractmethod
    def teardown(self) -> None:
        """Release the room. Called from a `finally` — must not raise."""

    def execution_metadata(self) -> dict:
        """Provider-specific sandbox/container identifiers for audit trails.

        Called after setup(); persisted on the trial trajectory before teardown.
        """
        return {}

    @classmethod
    def capabilities(cls) -> "BackendCapabilities":
        """What this backend can enforce. Checked before a sandbox is created.

        The default claims nothing, so a backend that forgets to declare gets
        tasks refused with a clear message rather than accepted and silently run
        with a weaker policy than the task asked for.
        """
        from app.services.backend_capabilities import BackendCapabilities

        return BackendCapabilities()


# Standard container paths (linux).
STD_DIRS = [
    "/data",
    "/app",
    "/tests",
    "/solution",
    "/logs/agent",
    "/logs/artifacts",
    "/logs/verifier",
]
