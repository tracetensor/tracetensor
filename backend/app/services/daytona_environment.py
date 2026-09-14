"""Daytona cloud sandbox backend — Phase 1 (real API adapter, no local Docker)."""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import time
from pathlib import Path

from app.services.daytona_client import call_with_retry
from app.services.environment import STD_DIRS, BaseEnvironment, ExecResult

log = logging.getLogger("tracetensor.environment.daytona")


def _env_value(name: str) -> str | None:
    """Deferred re-export of `backend_catalog.env_value`.

    Imported inside the function, not at module scope: `backend_catalog` calls
    `register_all_backends()` on import, which imports *this* module to register
    the Daytona adapter. A module-level import here closes that loop, so
    importing `daytona_environment` before `backend_catalog` raised ImportError
    on a partially initialised module. Only the import order made it work.
    """
    from app.services.backend_catalog import env_value

    return env_value(name)

# Daytona org limits (GiB for memory/disk).
_MAX_CPU = 4
_MAX_MEMORY_GIB = 8
_MAX_DISK_GIB = 10


def _dynamic_network_allowed() -> bool:
    """Whether this deployment may switch a running sandbox's network policy.

    `sandbox.update_network_settings` exists and `set_network` below implements
    it, but whether it is *permitted* is an account-level policy, not a property
    of the code. Probed against a real account, both directions were refused:

        Failed to update network settings: Network access is restricted and
        cannot be overridden at the sandbox level.

    So the default is False — a task with a phase network override is refused in
    milliseconds instead of after a sandbox has been created and paid for. An
    operator whose plan does allow it opts back in with
    TRACETENSOR_DAYTONA_DYNAMIC_NETWORK=1; the implementation is already there.
    """
    return os.getenv("TRACETENSOR_DAYTONA_DYNAMIC_NETWORK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _require_daytona_sdk():
    try:
        from daytona import CreateSandboxFromImageParams, Daytona, DaytonaConfig, Resources
        from daytona.common.image import Image
    except ImportError as exc:
        raise RuntimeError(
            "daytona SDK is not installed — run: pip install daytona"
        ) from exc
    return CreateSandboxFromImageParams, Daytona, DaytonaConfig, Resources, Image


class DaytonaEnvironment(BaseEnvironment):
    """Run a trial inside a Daytona sandbox (linux/amd64 container in the cloud)."""

    def __init__(
        self,
        task_dir: Path,
        image_tag: str | None = None,
        docker_image: str | None = None,
        build_timeout: float = 600.0,
        network_mode: str = "public",
        allowed_hosts: list | None = None,
        cpus: int | None = None,
        memory_mb: int | None = None,
        storage_mb: int | None = None,
        workdir: str | None = None,
        user: str | None = None,
        env: dict | None = None,
        image_prebuilt: bool = False,
        dockerfile_dir: str = "environment",
        platform: str | None = None,
    ):
        self.task_dir = task_dir
        self.docker_image = docker_image
        self.dockerfile_dir = dockerfile_dir
        self.build_timeout = build_timeout
        self.network_mode = network_mode
        self.allowed_hosts = allowed_hosts or []
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.storage_mb = storage_mb
        self.workdir = workdir or "/app"
        self.user = user
        self.env = env or {}
        self.platform = platform
        self._client = None
        self._sandbox = None
        self._snapshot: str | None = None
        self._snapshot_disabled = False

    def _daytona(self):
        if self._client is not None:
            return self._client
        _require_daytona_sdk()
        api_key = _env_value("DAYTONA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DAYTONA_API_KEY is not set — add it to backend/.env before using --backend daytona."
            )
        api_url = os.getenv("DAYTONA_API_URL") or os.getenv("DAYTONA_API_URL_FILE")
        if api_url and os.getenv("DAYTONA_API_URL_FILE") and not os.getenv("DAYTONA_API_URL"):
            api_url = _env_value("DAYTONA_API_URL")
        # Shared per credentials, not per environment — a dataset run creates one
        # environment per trial and would otherwise open a connection pool for each.
        from app.services.daytona_client import get_client

        self._client = get_client(api_key, api_url)
        return self._client

    def _task_image(self):
        _, _, _, _, Image = _require_daytona_sdk()
        if self.docker_image:
            log.info("using_remote_image image=%s", self.docker_image)
            return self.docker_image
        dockerfile = self.task_dir / self.dockerfile_dir / "Dockerfile"
        if not dockerfile.exists():
            raise RuntimeError(
                f"No Dockerfile in {self.dockerfile_dir}/ and no docker_image specified."
            )
        return Image.from_dockerfile(dockerfile)

    def _resource_shape(self) -> tuple[int, int, int]:
        """(cpu, memory GiB, disk GiB), clamped to the org's ceilings.

        Split out from `_resources` because the snapshot name has to include
        these: Daytona bakes resources into a snapshot and a sandbox created
        from one cannot override them, so two tasks differing only in requested
        memory must not share a snapshot.
        """
        cpu = min(max(int(self.cpus or 1), 1), _MAX_CPU)
        mem_gib = min(max(int((self.memory_mb or 1024) / 1024), 1), _MAX_MEMORY_GIB)
        disk_gib = min(max(int((self.storage_mb or 3072) / 1024), 1), _MAX_DISK_GIB)
        return cpu, mem_gib, disk_gib

    def _resources(self):
        _, _, _, Resources, _ = _require_daytona_sdk()
        cpu, mem_gib, disk_gib = self._resource_shape()
        return Resources(cpu=cpu, memory=mem_gib, disk=disk_gib)

    def _resolve_snapshot(self) -> str | None:
        """Name of a reusable snapshot for this task, or None to build from image.

        A task pinned to a registry image needs no snapshot — Daytona pulls that
        directly, and wrapping it would add a build step to save nothing.
        """
        if self.docker_image or self._snapshot_disabled:
            return None
        from app.services.daytona_snapshots import ensure_snapshot, snapshot_name

        cpu, mem_gib, disk_gib = self._resource_shape()
        context = self.task_dir / self.dockerfile_dir
        name = snapshot_name(context, cpu=cpu, memory_gib=mem_gib, disk_gib=disk_gib)
        ok = ensure_snapshot(
            self._daytona(),
            name,
            self._task_image(),
            self._resources(),
            timeout=self.build_timeout,
        )
        if not ok:
            # Per environment, so a later trial still re-probes. Deliberately not
            # process-wide: one failure can't distinguish "this account has no
            # snapshots" from "that build happened to fail", and latching the
            # pessimistic answer globally would demote every remaining trial off
            # the fast path for the rest of the process. The re-probe is one GET.
            self._snapshot_disabled = True
            return None
        return name

    def build(self) -> None:
        """Build the task image once into a reusable snapshot.

        Called by `prebuild` before trials fan out, and again from `setup` so a
        skipped or failed prebuild costs a slower first trial rather than
        silently reverting every trial to its own build.
        """
        self._snapshot = self._resolve_snapshot()

    def setup(self) -> None:
        if self.network_mode == "allowlist":
            raise RuntimeError(
                "network_mode 'allowlist' is not supported on Daytona yet. Use 'public' or 'no-network'."
            )
        CreateSandboxFromImageParams, _, _, _, _ = _require_daytona_sdk()
        if self._snapshot is None:
            self.build()  # prebuild may have been skipped; mirrors DockerEnvironment
        # Keep sandbox alive through long agent runs; default Daytona idle stop is aggressive.
        auto_stop_min = max(30, int(self.build_timeout // 60) + 45)
        common = {
            "network_block_all": self.network_mode == "no-network",
            "auto_stop_interval": auto_stop_min,
            # A create whose response was lost leaves a sandbox nobody holds a
            # handle to. Teardown can't reach it, so give it a deadline of its
            # own rather than let it bill until someone notices.
            "auto_delete_interval": auto_stop_min * 2,
            "env_vars": dict(self.env) or None,
        }
        if self._snapshot:
            from daytona import CreateSandboxFromSnapshotParams

            # No `resources` here by design: they were baked into the snapshot,
            # and this params type has no field to override them with.
            params = CreateSandboxFromSnapshotParams(snapshot=self._snapshot, **common)
        else:
            params = CreateSandboxFromImageParams(
                image=self._task_image(), resources=self._resources(), **common
            )
        log.info(
            "daytona_create_sandbox task=%s network=%s snapshot=%s",
            self.task_dir,
            self.network_mode,
            self._snapshot or "(none — building from image)",
        )
        self._sandbox = call_with_retry(
            lambda: self._daytona().create(params), what="sandbox.create"
        )
        self._sandbox.wait_for_sandbox_start(timeout=self.build_timeout)
        log.info(
            "daytona_sandbox_started",
            extra={
                "sandbox_id": getattr(self._sandbox, "id", None),
                "target": getattr(self._sandbox, "target", None),
            },
        )
        # Bootstrap from "/" — every other exec runs with cwd=self.workdir, and
        # the workdir does not exist yet in an image that never declared it (a
        # separate verifier's tests/Dockerfile typically doesn't). Daytona reports
        # a missing cwd as `fork/exec /usr/bin/bash: no such file or directory`,
        # so the command that was supposed to CREATE the workdir failed for want
        # of the workdir, and every later exec failed the same way.
        bootstrap = self._exec_root(
            f"mkdir -p {' '.join(shlex.quote(p) for p in STD_DIRS)} {shlex.quote(self.workdir)}",
            cwd="/",
        )
        # Checked, unlike before: these ran unverified, so a sandbox that could
        # not exec at all still completed setup and failed later somewhere
        # unrelated — which is exactly how the missing-workdir bug stayed hidden.
        if bootstrap.exit_code != 0:
            detail = (bootstrap.stderr or "") + (bootstrap.stdout or "")
            raise RuntimeError(
                f"Could not prepare the Daytona sandbox's directories "
                f"(exit {bootstrap.exit_code}): {detail.strip()[-300:] or '(no output)'}"
            )
        self._exec_root("chmod 0755 /logs/verifier")
        self._exec_root("chmod 0777 /logs/agent /logs/artifacts")

    def _require_sandbox(self):
        if self._sandbox is None:
            raise RuntimeError("Daytona sandbox not started — call setup() first.")
        return self._sandbox

    def _exec_root(
        self, command: str, timeout: float | None = None, cwd: str | None = None
    ) -> ExecResult:
        return self.exec(command, phase="setup", timeout=timeout, as_user="0", cwd=cwd)

    def exec(
        self,
        command: str,
        phase: str = "agent",
        timeout: float | None = None,
        as_user: str | None = None,
        env: dict | None = None,
        cwd: str | None = None,
    ) -> ExecResult:
        sandbox = self._require_sandbox()
        start = time.time()
        user = as_user if as_user is not None else self.user
        run_cmd = command
        if user is not None and str(user) not in ("0", "root"):
            run_cmd = f"runuser -u {shlex.quote(str(user))} -- bash -c {shlex.quote(command)}"
        exec_timeout = int(timeout) if timeout is not None else None
        merged_env = {**(self.env or {}), **(env or {})} or None
        try:
            response = sandbox.process.exec(
                run_cmd,
                cwd=cwd or self.workdir,
                env=merged_env,
                timeout=exec_timeout,
            )
            exit_code = response.exit_code if response.exit_code is not None else 1
            stdout = response.result or ""
            if response.artifacts and response.artifacts.stdout and not stdout:
                stdout = response.artifacts.stdout
            stderr = ""
            if response.additional_properties:
                stderr = str(response.additional_properties.get("stderr") or "")
            return ExecResult(
                command=command,
                exit_code=int(exit_code),
                stdout=stdout,
                stderr=stderr,
                duration_s=time.time() - start,
                phase=phase,
            )
        except Exception as exc:
            msg = str(exc)
            if "timeout" in msg.lower():
                return ExecResult(
                    command=command,
                    exit_code=124,
                    stdout="",
                    stderr=f"TIMEOUT after {timeout}s",
                    duration_s=time.time() - start,
                    phase=phase,
                )
            raise

    def read_file(self, path: str) -> str | None:
        sandbox = self._require_sandbox()
        try:
            data = sandbox.fs.download_file(path)
        except Exception:
            return None
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return None

    def write_file(self, path: str, content: bytes) -> None:
        sandbox = self._require_sandbox()
        parent = posixpath.dirname(path) or "/"
        sandbox.fs.create_folder(parent, "755")
        sandbox.fs.upload_file(content, path)

    def copy_in(self, src_dir: Path, dest: str) -> None:
        sandbox = self._require_sandbox()
        sandbox.fs.create_folder(dest, "755")
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src_dir).as_posix()
            remote = f"{dest.rstrip('/')}/{rel}"
            parent = posixpath.dirname(remote) or "/"
            if parent not in ("/", dest.rstrip("/")):
                sandbox.fs.create_folder(parent, "755")
            sandbox.fs.upload_file(path.read_bytes(), remote)

    def set_network(self, mode: str | None, allowed_hosts: list[str] | None = None) -> None:
        """Switch a live sandbox's egress between phases.

        Daytona exposes this on the running sandbox, so a task can grant the
        agent network access and deny it to the verifier — the same phase
        override Docker gets from connect/disconnect.
        """
        if mode is None or mode == self.network_mode:
            return
        if mode == "allowlist":
            # The API takes CIDR/domain allow lists, so this is reachable — but
            # unverified enforcement is worse than an honest refusal, and
            # `capabilities()` says network_allowlist=False to match.
            raise RuntimeError(
                "allowlist network mode is not implemented on Daytona yet. "
                "Use 'public' or 'no-network'."
            )
        if mode not in ("public", "no-network"):
            raise RuntimeError(f"Unknown network mode: {mode}")
        sandbox = self._require_sandbox()
        block = mode == "no-network"
        call_with_retry(
            lambda: sandbox.update_network_settings(network_block_all=block),
            what=f"update_network_settings({mode})",
        )
        log.info("daytona_network_switched from=%s to=%s", self.network_mode, mode)
        self.network_mode = mode

    def transfer_from(self, other: BaseEnvironment, paths: list[str]) -> list[str]:
        """Copy paths out of the agent's sandbox into this grading sandbox.

        Returns the requested paths that did NOT exist at the source, so the
        caller can warn rather than score a silent 0 for a missing artifact.

        Moved as one tar stream rather than walked with the filesystem API: a
        declared artifact is usually a directory, and tar carries the tree,
        permissions and symlinks in a single round trip instead of one API call
        per file.
        """
        if not isinstance(other, DaytonaEnvironment):
            raise TypeError(
                "transfer_from requires a DaytonaEnvironment source, "
                f"got {type(other).__name__}"
            )
        if not paths:
            return []

        present, missing = [], []
        for path in paths:
            probe = other._exec_root(f"test -e {shlex.quote(path)}")
            (present if probe.exit_code == 0 else missing).append(path)
        if not present:
            return missing

        # tar members are stored relative to / so they restore to the same
        # absolute locations in the destination.
        archive = "/tmp/tt_transfer.tar.gz"
        members = " ".join(shlex.quote(p.lstrip("/")) for p in present)
        packed = other._exec_root(f"tar -czf {archive} -C / {members}")
        if packed.exit_code != 0:
            detail = (packed.stderr or "") + (packed.stdout or "")
            raise RuntimeError(
                f"Could not pack artifacts in the agent sandbox "
                f"(exit {packed.exit_code}): {detail.strip()[-400:] or '(no output)'}"
            )

        blob = other._download_bytes(archive)
        if blob is None:
            raise RuntimeError("Could not download the artifact archive from the agent sandbox.")
        self.write_file(archive, blob)
        unpacked = self._exec_root(f"tar -xzf {archive} -C /")
        if unpacked.exit_code != 0:
            # Daytona routes command output to stdout and usually leaves stderr
            # empty, so reporting stderr alone loses the reason entirely.
            detail = (unpacked.stderr or "") + (unpacked.stdout or "")
            raise RuntimeError(
                f"Could not unpack artifacts in the verifier sandbox "
                f"(exit {unpacked.exit_code}): {detail.strip()[-400:] or '(no output)'}"
            )

        other._exec_root(f"rm -f {archive}")
        self._exec_root(f"rm -f {archive}")
        log.info("daytona_transfer moved=%d missing=%d", len(present), len(missing))
        return missing

    def _download_bytes(self, path: str) -> bytes | None:
        sandbox = self._require_sandbox()
        try:
            data = call_with_retry(
                lambda: sandbox.fs.download_file(path), what=f"fs.download_file({path})"
            )
        except Exception:
            log.exception("daytona_download_failed path=%s", path)
            return None
        return data if isinstance(data, bytes) else None

    @classmethod
    def capabilities(cls):
        from app.services.backend_capabilities import BackendCapabilities

        return BackendCapabilities(
            network_isolation=True,  # network_block_all at create time
            network_allowlist=False,  # API supports it; enforcement unverified here
            dynamic_network=_dynamic_network_allowed(),
            separate_verifier=True,  # tar round trip between sandboxes
        )

    def execution_metadata(self) -> dict:
        sandbox = self._sandbox
        meta: dict = {
            "provider": "daytona",
            "network_mode": self.network_mode,
        }
        if self.docker_image:
            meta["docker_image"] = self.docker_image
        # Recorded so a run's cost/latency is explainable after the fact: a trial
        # that built its own image and one that reused a snapshot look identical
        # in the timings alone.
        meta["snapshot"] = self._snapshot or None
        meta["snapshot_reused"] = bool(self._snapshot)
        if sandbox is None:
            return meta
        state = getattr(sandbox, "state", None)
        meta.update(
            {
                "sandbox_id": getattr(sandbox, "id", None),
                "name": getattr(sandbox, "name", None),
                "target": getattr(sandbox, "target", None),
                "state": str(state) if state is not None else None,
                "cpu": getattr(sandbox, "cpu", None),
                "memory_gib": getattr(sandbox, "memory", None),
                "disk_gib": getattr(sandbox, "disk", None),
                "network_block_all": getattr(sandbox, "network_block_all", None),
                "created_at": getattr(sandbox, "created_at", None),
            }
        )
        return {k: v for k, v in meta.items() if v is not None}

    def teardown(self) -> None:
        sandbox = self._sandbox
        self._sandbox = None
        if sandbox is None:
            return
        try:
            sandbox.delete()
        except Exception:
            log.exception("daytona_delete_failed", extra={"sandbox_id": getattr(sandbox, "id", "?")})


def ping_daytona_api() -> tuple[bool, str]:
    """Lightweight API check — lists sandboxes, does not create one."""
    try:
        _, Daytona, DaytonaConfig, _, _ = _require_daytona_sdk()
    except RuntimeError as exc:
        return False, str(exc)
    api_key = _env_value("DAYTONA_API_KEY")
    if not api_key:
        return False, "DAYTONA_API_KEY is not set"
    try:
        client = Daytona(DaytonaConfig(api_key=api_key))
        next(iter(client.list()), None)
    except StopIteration:
        pass
    except Exception as exc:
        return False, f"Daytona API error: {exc}"[:200]
    return True, "Daytona API reachable"
