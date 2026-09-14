"""Placeholder cloud sandbox backends (Phase 0).

Registered so `--backend daytona` (etc.) resolves through the same factory as
Docker, but trial execution is deferred to Phase 1. `build()` is a no-op so
prebuild can run; `setup()` fails with an actionable message.
"""

from __future__ import annotations

from pathlib import Path

from app.services.environment import BaseEnvironment, ExecResult


class PlannedCloudEnvironment(BaseEnvironment):
    """Cloud provider adapter stub — same lifecycle contract, no remote calls yet."""

    PROVIDER_ID: str = "cloud"

    def __init__(self, task_dir: Path, **kwargs: object) -> None:
        self.task_dir = task_dir
        self._kwargs = kwargs

    def _blocked(self, phase: str) -> RuntimeError:
        pid = self.PROVIDER_ID
        return RuntimeError(
            f"{pid} backend is registered but trial execution is not implemented yet (Phase 1). "
            f"Preflight: tracetensor backends preflight {pid}"
        )

    def build(self) -> None:
        return

    def setup(self) -> None:
        raise self._blocked("setup")

    def exec(
        self,
        command: str,
        phase: str = "agent",
        timeout: float | None = None,
        as_user: str | None = None,
        env: dict | None = None,
    ) -> ExecResult:
        raise self._blocked("exec")

    def read_file(self, path: str) -> str | None:
        raise self._blocked("read_file")

    def write_file(self, path: str, content: bytes) -> None:
        raise self._blocked("write_file")

    def copy_in(self, src_dir: Path, dest: str) -> None:
        raise self._blocked("copy_in")

    def set_network(self, mode: str | None) -> None:
        raise self._blocked("set_network")

    def transfer_from(self, other: BaseEnvironment, paths: list[str]) -> list[str]:
        raise self._blocked("transfer_from")

    def teardown(self) -> None:
        return
