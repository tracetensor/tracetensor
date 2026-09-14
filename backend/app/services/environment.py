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

import subprocess
import time
from pathlib import Path
from typing import Callable

# Re-exported so `from app.services.environment import BaseEnvironment, …`
# keeps working everywhere — the base contract lives in environment_base.
from app.services.environment_base import (  # noqa: F401
    STD_DIRS,
    BaseEnvironment,
    ExecResult,
)

# ----------------------------------------------------------------------
# Docker environment (production)
# ----------------------------------------------------------------------


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
        if self.network_mode == "allowlist":
            # Bridge network is used; iptables rules are applied in _apply_allowlist_rules()
            # after the container starts. NET_ADMIN capability is added unconditionally
            # in setup() so it is always available for phase switching.
            return []
        raise RuntimeError(f"Unknown network_mode: {self.network_mode!r}")

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
            pull_args = [self._CLI, "pull"]
            if self.platform:
                pull_args += ["--platform", self.platform]
            pull_args.append(self.docker_image)
            pull = self._run(pull_args, timeout=self.build_timeout)
            if pull.returncode != 0:
                hint = ""
                err = (pull.stderr or pull.stdout or "").lower()
                if "manifest" in err or "platform" in err:
                    hint = (
                        "\nHint: amd64-only images on Apple Silicon need "
                        '[environment].platform = "linux/amd64" in task.toml or '
                        "tracetensor run --platform linux/amd64."
                    )
                elif "not found" in err or "repository does not exist" in err:
                    has_dockerfile = (self.task_dir / self.dockerfile_dir / "Dockerfile").exists()
                    if has_dockerfile:
                        hint = (
                            f"\nHint: docker_image = {self.docker_image!r} tells TraceTensor to "
                            "pull from a registry instead of building from environment/Dockerfile. "
                            "Remove docker_image from task.toml to build locally."
                        )
                raise RuntimeError(
                    f"docker pull failed for {self.docker_image!r}:\n"
                    f"{pull.stderr[-2000:]}{hint}"
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
        run_args += self._network_args()
        run_args += self._resource_args()
        # NET_ADMIN lets iptables manage egress rules inside the container.
        # Required for allowlist mode — added unconditionally so a no-network
        # container that later switches to allowlist (via agent phase override)
        # already has the capability at startup time.
        run_args += ["--cap-add", "NET_ADMIN"]
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

        # For allowlist mode, apply iptables egress rules now that the container
        # is running. Resolves allowed hostnames on the HOST (before any rules)
        # and injects per-IP ACCEPT rules, then drops everything else.
        if self.network_mode == "allowlist":
            self._apply_allowlist_rules(self.allowed_hosts)

    def _apply_allowlist_rules(self, allowed_hosts: list[str]) -> None:
        """Enforce egress allowlist inside the container using iptables.

        Resolves each allowed hostname to its current IP(s) on the HOST (before
        any rules are in place), then injects iptables OUTPUT rules that ACCEPT
        only those IPs plus loopback, established connections, and DNS. Everything
        else is DROPped. Requires NET_ADMIN — ensured by _network_args() when
        network_mode == 'allowlist'.

        Best-effort: logs a warning if iptables is unavailable in the image
        rather than failing the whole trial, so tasks still run (with full egress)
        and the operator sees a clear warning.
        """
        import logging
        import socket

        log = logging.getLogger("tracetensor.environment")

        # Resolve hostnames → IPs on the host (unaffected by container rules).
        allowed_ips: set[str] = set()
        for host in allowed_hosts:
            host = host.lstrip("*.")  # strip wildcard prefix for resolution
            try:
                for _, _, _, _, addr in socket.getaddrinfo(host, None):
                    ip = addr[0]
                    if ":" not in ip:  # IPv4 only for now
                        allowed_ips.add(ip)
            except Exception as exc:
                log.warning("allowlist_resolve_failed", extra={"host": host, "error": str(exc)})

        if not allowed_ips and allowed_hosts:
            log.warning("allowlist_no_ips_resolved", extra={"hosts": allowed_hosts})

        # Build a single shell command: install iptables if absent, then set rules.
        accept_rules = "".join(
            f"iptables -A OUTPUT -d {ip} -j ACCEPT && " for ip in sorted(allowed_ips)
        )
        # Try to use iptables directly; only install if needed (saves ~30s when pre-installed)
        rules_cmd = (
            "which iptables >/dev/null 2>&1 || ("
            "apt-get update -qq 2>/dev/null && "
            "apt-get install -yq iptables 2>/dev/null || true) && "
            # Allow loopback
            "iptables -A OUTPUT -o lo -j ACCEPT && "
            # Allow already-established connections (responses to initiated traffic)
            "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT && "
            # Allow DNS so hostname resolution works inside the container
            "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT && "
            "iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT && "
            # Allow each resolved allowed IP
            + accept_rules +
            # Drop everything else
            "iptables -P OUTPUT DROP"
        )

        result = self._run(
            [self._CLI, "exec", "-u", "0", self._cid, "bash", "-c", rules_cmd]
        )
        if result.returncode != 0:
            log.warning(
                "allowlist_iptables_failed",
                extra={
                    "returncode": result.returncode,
                    "stderr": (result.stderr or "")[:300],
                    "note": "Container running with FULL egress — allowlist not enforced",
                },
            )
        else:
            log.info(
                "allowlist_applied",
                extra={"allowed_hosts": allowed_hosts, "resolved_ips": sorted(allowed_ips)},
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

    @classmethod
    def capabilities(cls):
        from app.services.backend_capabilities import BackendCapabilities

        return BackendCapabilities(
            network_isolation=True,  # --network none
            # allowlist needs the egress-control sidecar that _network_args
            # rejects; claimed only when that exists.
            network_allowlist=False,
            dynamic_network=True,  # network connect/disconnect on a live container
            separate_verifier=True,  # docker cp between containers
        )

    def copy_in(self, src_dir: Path, dest: str) -> None:
        """Copy the CONTENTS of src_dir into dest, recursively.

        The trailing `/.` is what makes this the directory's contents rather than
        the directory itself, matching the old per-file loop's layout. That loop
        also filtered to `is_file()`, so any subdirectory was skipped without a
        word — a nested agent project or task fixture arrived half-copied and the
        failure surfaced much later as a missing import. Daytona's copy_in has
        always been recursive (it walks with rglob), so this also removes a
        behaviour difference between the two backends.
        """
        if not src_dir.is_dir():
            raise RuntimeError(f"copy_in source is not a directory: {src_dir}")
        self._run([self._CLI, "exec", "-u", "0", self._cid, "mkdir", "-p", dest])
        proc = self._run([self._CLI, "cp", f"{src_dir}/.", f"{self._cid}:{dest}"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(f"Failed to copy {src_dir} into the container at {dest}: {detail}")

    def set_network(self, mode: str | None, allowed_hosts: list[str] | None = None) -> None:
        """Switch the running container's network for a phase override.

        Supports public<->no-network via docker network connect/disconnect.
        Supports switching to `allowlist` by connecting to bridge then applying
        iptables rules. Switching FROM allowlist back to no-network clears the
        bridge connection.

        `allowed_hosts` overrides self.allowed_hosts when switching to allowlist
        mode — use this to pass per-phase allowed_hosts from task.toml [agent].
        """
        if mode is None or mode == self.network_mode:
            return
        if mode == "no-network":
            proc = self._run([self._CLI, "network", "disconnect", "bridge", self._cid])
        elif mode == "public":
            # A container started with `--network none` is attached to the `none`
            # network, and the daemon refuses to add a second network while that
            # attachment stands ("cannot be connected to multiple networks with
            # one of the networks in private (none) mode"). Detach it first. This
            # is a no-op — and a harmless non-zero exit — when the container was
            # started on bridge and later disconnected, so its result is ignored;
            # only the connect below decides success.
            self._run([self._CLI, "network", "disconnect", "none", self._cid])
            proc = self._run([self._CLI, "network", "connect", "bridge", self._cid])
        elif mode == "allowlist":
            # Connect to bridge first (same as public), then apply iptables rules.
            # Use the phase-specific allowed_hosts if provided, else fall back to
            # the container-level self.allowed_hosts set at construction time.
            self._run([self._CLI, "network", "disconnect", "none", self._cid])
            proc = self._run([self._CLI, "network", "connect", "bridge", self._cid])
            if proc.returncode == 0:
                self.network_mode = mode
                self._apply_allowlist_rules(allowed_hosts if allowed_hosts is not None else self.allowed_hosts)
                return
        else:
            raise RuntimeError(f"Unknown network mode: {mode}")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(
                f"Failed to switch container network from {self.network_mode!r} to {mode!r}: {detail}"
            )
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

    def execution_metadata(self) -> dict:
        meta: dict = {"network_mode": self.network_mode}
        if self.container_id:
            meta["container_id"] = self.container_id
        if self.image_tag:
            meta["image"] = self.image_tag
        if self.docker_image:
            meta["docker_image"] = self.docker_image
        if self.platform:
            meta["platform"] = self.platform
        return meta


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


#: Backends whose module is imported the first time one is actually built.
#: Keyed the same as _BACKENDS; the resolved class is promoted into _BACKENDS.
_LAZY_BACKENDS: dict[str, Callable[[], type[BaseEnvironment]]] = {}


def register_environment(name: str, cls: type[BaseEnvironment]) -> None:
    """Register a custom environment backend under `name` (idempotent overwrite)."""
    if not issubclass(cls, BaseEnvironment):
        raise TypeError(f"{cls!r} must subclass BaseEnvironment")
    _BACKENDS[name] = cls


def register_environment_lazy(name: str, loader: Callable[[], type[BaseEnvironment]]) -> None:
    """Register a backend by a loader that imports its class on first use.

    For backends whose module is expensive or circular to import at registration
    time — a cloud adapter pulling in an optional vendor SDK, or one that imports
    this module itself. Eagerly importing Daytona here meant `import
    daytona_environment` before `backend_catalog` raised ImportError on a
    partially initialised module; only the import order made it work.

    The backend still appears in `available_backends()` before its first use, so
    catalogs and `--backend` validation behave identically to an eager one.
    """
    _LAZY_BACKENDS[name] = loader


def _resolve_backend(name: str) -> type[BaseEnvironment] | None:
    cls = _BACKENDS.get(name)
    if cls is not None:
        return cls
    loader = _LAZY_BACKENDS.get(name)
    if loader is None:
        return None
    cls = loader()
    if not isinstance(cls, type) or not issubclass(cls, BaseEnvironment):
        raise TypeError(f"Lazy backend {name!r} loaded {cls!r}, which is not a BaseEnvironment.")
    _BACKENDS[name] = cls  # promote so the import happens once
    return cls


def available_backends() -> list[str]:
    return sorted(set(_BACKENDS) | set(_LAZY_BACKENDS))


def make_environment(backend: str, task_dir: Path, **kwargs: object) -> BaseEnvironment:
    """Factory. Resolves `backend` through the plug-in registry (see
    register_environment). Docker is the default, real-isolation backend; Podman
    ships as a drop-in alternative."""
    cls = _resolve_backend(backend)
    if cls is None:
        raise ValueError(
            f"Unsupported backend '{backend}'. Available: {', '.join(available_backends())}."
        )
    return cls(task_dir, **kwargs)


from app.services import backend_catalog as _backend_catalog  # noqa: E402,F401 — register cloud stubs
