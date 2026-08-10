"""
Execution environments — the Examination Room itself.

Doctor analogy: the room the patient (agent) works in. The room exposes a
uniform interface (exec a command, read/write a file), so the trial runner
doesn't care whether it's a real Docker container or a local sandbox dir.

Two implementations:
  DockerEnvironment  — production. Builds the task's image, runs a container,
                       execs commands inside it. Uses Docker containers.
  PodmanEnvironment  — drop-in alternative using Podman.

Both return ExecResult objects so the recorder captures identical trajectories.

SECURITY NOTE — Docker socket and image trust:
  This service has access to the host Docker socket, so it effectively has
  root-equivalent privileges on the host. Two consequences operators must
  understand:

  1. docker_image: a task.toml can specify an arbitrary remote image
     (e.g. gcr.io/someone/image). TraceTensor will pull and run it. There is
     no domain allowlist — operators who accept tasks from untrusted authors
     should review task.toml before running.

  2. docker build: runs with full network access. A task's Dockerfile can
     RUN arbitrary commands with egress (curl, wget, etc.) during the build
     phase. The --network flag on `docker build` is not enforced here because
     many legitimate tasks need to install packages. Again, review Dockerfiles
     from untrusted sources before running.
"""

from __future__ import annotations

import abc
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


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
    def set_network(self, mode: str | None) -> None:
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


# ----------------------------------------------------------------------
# Docker environment (production)
# ----------------------------------------------------------------------
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


class DockerEnvironment(BaseEnvironment):
    """Builds the task image and runs a container, applying the task's
    environment config (network, resources, workdir, user, env vars) as
    the task's environment config dictates. Commands run via `docker exec`.

    Network (Network semantics):
      public      -> default bridge (full egress)
      no-network  -> --network none
      allowlist   -> rejected at setup (needs an nftables egress sidecar; not
                     yet built). Best practice is to reject rather than run weaker.

    The container CLI is `_CLI` (a class attr) so a drop-in, CLI-compatible
    runtime (e.g. Podman — see PodmanEnvironment) is just a one-line subclass.
    """

    _CLI = "docker"

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
        import hashlib

        self.task_dir = task_dir
        self.docker_image = docker_image
        self.dockerfile_dir = dockerfile_dir  # "environment" (agent) or "tests" (separate verifier)
        # Stable tag per (task, dockerfile_dir): subsequent trials hit Docker's
        # layer cache → the image is effectively built once, fresh container each trial.
        stable = hashlib.sha1(f"{task_dir.resolve()}:{dockerfile_dir}".encode()).hexdigest()[:12]
        self.image_tag = image_tag or f"tracetensor/{dockerfile_dir}:{stable}"
        self.image_prebuilt = image_prebuilt
        self.build_timeout = build_timeout
        self.network_mode = network_mode
        self.allowed_hosts = allowed_hosts or []
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.storage_mb = storage_mb
        self.workdir = workdir or "/app"
        self.user = user  # agent OS user (None = image default)
        self.env = env or {}
        self.platform = platform  # docker --platform (e.g. linux/amd64), None = host default
        self.container_id: str | None = None

    @property
    def _cid(self) -> str:
        """The container id, guaranteed non-None. Every exec/cp path runs only
        after setup() has started the container; this asserts that invariant (and
        gives mypy a `str`, not `str | None`, for the docker-arg lists)."""
        if self.container_id is None:
            raise RuntimeError("Container not started — call setup() first.")
        return self.container_id

    def _run(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def _network_args(self) -> list[str]:
        if self.network_mode == "no-network":
            return ["--network", "none"]
        if self.network_mode == "public":
            return []  # default bridge = full egress
        # allowlist: needs an egress-control sidecar (nftables). Not built yet —
        # reject the trial rather than silently run with a weaker policy.
        raise RuntimeError(
            "network_mode 'allowlist' requires an egress-control sidecar that "
            "TraceTensor doesn't implement yet. Use 'public' or 'no-network', "
            "or run this task on a provider that supports allowlist."
        )

    def _resource_args(self) -> list[str]:
        args: list[str] = []
        if self.cpus:
            args += ["--cpus", str(self.cpus)]
        if self.memory_mb:
            args += ["--memory", f"{self.memory_mb}m"]
        if self.storage_mb:
            # Best-effort; only works on storage drivers with quota support.
            args += ["--storage-opt", f"size={self.storage_mb}m"]
        return args

    def build(self) -> None:
        """Build the task image (idempotent: repeat calls hit Docker's layer
        cache). Split out of setup() so a job can warm the image ONCE before it
        fans out parallel trials — otherwise N trials would each launch a cold
        build of the same tag simultaneously and race. A no-op when a prebuilt
        image tag is supplied or the image was already warmed."""
        if self.docker_image:
            self.image_tag = self.docker_image
            import logging

            logging.getLogger("tracetensor.environment").warning(
                "using_remote_image",
                extra={"image": self.docker_image},
            )
            return
        if self.image_prebuilt:
            return
        ctx = self.task_dir / self.dockerfile_dir
        dockerfile = ctx / "Dockerfile"
        if not dockerfile.exists():
            raise RuntimeError(
                f"No Dockerfile in {self.dockerfile_dir}/ and no docker_image specified."
            )
        plat = ["--platform", self.platform] if self.platform else []
        build = self._run(
            [self._CLI, "build", *plat, "-t", self.image_tag, "-f", str(dockerfile), str(ctx)],
            timeout=self.build_timeout,
        )
        if build.returncode != 0:
            raise RuntimeError(f"docker build failed:\n{build.stderr[-2000:]}")

    def setup(self) -> None:
        self.build()

        run_args = [self._CLI, "run", "-d", "--rm"]
        if self.platform:  # e.g. run an x86 SWE-Bench image on arm64 via emulation
            run_args += ["--platform", self.platform]
        run_args += self._network_args()  # may raise on allowlist
        run_args += self._resource_args()
        for k, v in self.env.items():  # environment variables
            # M-4: reject keys that aren't valid POSIX variable names — a key
            # containing '=' or newlines corrupts the -e parsing; an invalid key
            # is a misconfigured task.toml, not something we should silently pass.
            import re

            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(
                    f"Invalid environment variable name in task.toml: {k!r} — "
                    "must match [A-Za-z_][A-Za-z0-9_]*"
                )
            run_args += ["-e", f"{k}={v}"]
        run_args += [self.image_tag, "sleep", "3600"]

        run = self._run(run_args)
        if run.returncode != 0:
            raise RuntimeError(f"docker run failed:\n{run.stderr[-2000:]}")
        self.container_id = run.stdout.strip()

        # Standard dirs. /logs/verifier is root-owned and world-unwritable so a
        # non-root agent can't forge the reward (true isolation = separate mode).
        self._run([self._CLI, "exec", "-u", "0", self._cid, "mkdir", "-p", *STD_DIRS, self.workdir])
        self._run([self._CLI, "exec", "-u", "0", self._cid, "chmod", "0755", "/logs/verifier"])
        # Agent scratch/artifact dirs must be writable by the agent OS user (which
        # may be non-root). The built-in loop runs as root and never needed this;
        # an installed agent like mini-swe writes its trajectory to /logs/agent and
        # drops declared artifacts in /logs/artifacts as the (possibly non-root)
        # agent user — root-owned 0755 dirs would silently swallow both.
        self._run(
            [self._CLI, "exec", "-u", "0", self._cid, "chmod", "0777"]
            + ["/logs/agent", "/logs/artifacts"]
        )

    def exec(
        self,
        command: str,
        phase: str = "agent",
        timeout: float | None = None,
        as_user: str | None = None,
        env: dict | None = None,
    ) -> ExecResult:
        start = time.time()
        args = [self._CLI, "exec", "-w", self.workdir]
        user = as_user if as_user is not None else self.user
        if user is not None:
            args += ["-u", str(user)]
        # Inject env vars via `docker exec -e` so secrets (e.g. a model API key
        # for an installed agent) enter the process env, NOT the logged command.
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += [self._cid, "bash", "-c", command]
        try:
            proc = self._run(args, timeout=timeout)
            return ExecResult(
                command=command,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=time.time() - start,
                phase=phase,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                command=command,
                exit_code=124,
                stdout="",
                stderr=f"TIMEOUT after {timeout}s",
                duration_s=time.time() - start,
                phase=phase,
            )

    def read_file(self, path: str) -> str | None:
        proc = self._run([self._CLI, "exec", "-u", "0", self._cid, "cat", path])
        return proc.stdout if proc.returncode == 0 else None

    def write_file(self, path: str, content: bytes) -> None:
        # Write via `docker cp`, not a shell redirect. The path used to be
        # interpolated into `bash -c`, which shell-injects if it contains
        # metacharacters (a task-controlled artifact path could). docker cp and
        # mkdir take the path as an argv arg, so nothing is shell-parsed.
        import os
        import posixpath
        import tempfile

        parent = posixpath.dirname(path) or "/"
        self._run([self._CLI, "exec", "-u", "0", self._cid, "mkdir", "-p", parent])
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(content)
                tmp = tf.name
            self._run([self._CLI, "cp", tmp, f"{self._cid}:{path}"])
            # Keep the old semantics: files we write are root-owned.
            self._run([self._CLI, "exec", "-u", "0", self._cid, "chown", "0:0", path])
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    def copy_in(self, src_dir: Path, dest: str) -> None:
        self._run([self._CLI, "exec", "-u", "0", self._cid, "mkdir", "-p", dest])
        for f in src_dir.iterdir():
            if f.is_file():
                self._run([self._CLI, "cp", str(f), f"{self._cid}:{dest}/{f.name}"])

    def set_network(self, mode: str | None) -> None:
        """Switch the running container's network for a phase override.

        Supports public<->no-network via docker network connect/disconnect.
        `allowlist` phase overrides need the egress sidecar → rejected.
        """
        if mode is None or mode == self.network_mode:
            return
        if mode == "allowlist":
            raise RuntimeError("allowlist network phase override is not supported yet.")
        if mode == "no-network":
            self._run([self._CLI, "network", "disconnect", "bridge", self._cid])
        elif mode == "public":
            self._run([self._CLI, "network", "connect", "bridge", self._cid])
        self.network_mode = mode

    def transfer_from(self, other: "BaseEnvironment", paths: list[str]) -> list[str]:
        """Copy paths from another container into this one (agent → verifier).

        Used by the isolated (separate) verifier so it grades the agent's real
        output without ever sharing the agent's container. Returns the list of
        requested paths that were ABSENT in the source container (skipped), so
        the caller can warn — otherwise a missing artifact silently becomes a
        0.0 that looks just like a genuine failure.
        """
        import tempfile

        # Container-to-container copy only makes sense Docker→Docker (this is the
        # only backend anyway). A real raise, not an assert — `python -O` strips
        # asserts, and a future non-Docker env would then fail deep inside on a
        # missing `container_id` attribute instead of here.
        if not isinstance(other, DockerEnvironment):
            raise TypeError(
                f"transfer_from requires a DockerEnvironment source, got {type(other).__name__}"
            )
        skipped: list[str] = []
        for path in paths:
            with tempfile.TemporaryDirectory(prefix="tt_xfer_") as td:
                host = Path(td) / "item"
                got = self._run([self._CLI, "cp", f"{other.container_id}:{path}", str(host)])
                if got.returncode != 0:
                    skipped.append(path)  # path absent in the agent container
                    continue
                # Ensure the destination parent exists, then copy in.
                parent = str(Path(path).parent) or "/"
                self._run([self._CLI, "exec", "-u", "0", self._cid, "mkdir", "-p", parent])
                self._run([self._CLI, "cp", str(host), f"{self._cid}:{path}"])
        return skipped

    def teardown(self) -> None:
        if self.container_id:
            self._run([self._CLI, "kill", self.container_id])
            self.container_id = None


class PodmanEnvironment(DockerEnvironment):
    """Podman is a drop-in, daemonless, rootless-capable Docker CLI — so the whole
    DockerEnvironment lifecycle works verbatim; only the binary changes. This is
    the reference "second backend" showing how little it takes to add one: swap
    `_CLI`, register a name. (Podman must be installed; its default network differs
    slightly, so phase network overrides are best-effort.)"""

    _CLI = "podman"


# ----------------------------------------------------------------------
# Environment plug-in registry — the documented extension point.
# ----------------------------------------------------------------------
# A backend is any BaseEnvironment subclass implementing the lifecycle
# (build → setup → exec/read_file/write_file/copy_in/set_network/transfer_from →
# teardown). Register one by name and it's selectable via `--backend <name>` /
# make_environment(backend=<name>, …) everywhere. Third parties (a cloud sandbox
# provider, a local runtime) plug in the same way without touching the core.
_BACKENDS: dict[str, type[BaseEnvironment]] = {
    "docker": DockerEnvironment,
    "podman": PodmanEnvironment,
}


def register_environment(name: str, cls: type[BaseEnvironment]) -> None:
    """Register a custom environment backend under `name` (idempotent overwrite)."""
    if not issubclass(cls, BaseEnvironment):
        raise TypeError(f"{cls!r} must subclass BaseEnvironment")
    _BACKENDS[name] = cls


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


def make_environment(backend: str, task_dir: Path, **kwargs: object) -> BaseEnvironment:
    """Factory. Resolves `backend` through the plug-in registry (see
    register_environment). Docker is the default, real-isolation backend; Podman
    ships as a drop-in alternative."""
    cls = _BACKENDS.get(backend)
    if cls is None:
        raise ValueError(
            f"Unsupported backend '{backend}'. Available: {', '.join(available_backends())}."
        )
    return cls(task_dir, **kwargs)
