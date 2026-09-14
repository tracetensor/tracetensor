"""Execution backend catalog, preflight, and cloud backend registration."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.schemas.examine import BackendEntry
from app.services.environment import register_environment, register_environment_lazy
from app.services.planned_cloud_environment import PlannedCloudEnvironment

DEFAULT_BACKEND = "docker"
BackendKind = Literal["local", "cloud"]

_IMPLEMENTED_CLOUD = frozenset({"daytona"})


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightResult:
    backend: str
    ready: bool
    checks: tuple[PreflightCheck, ...]


@dataclass(frozen=True)
class BackendSpec:
    id: str
    label: str
    kind: BackendKind
    env_vars: tuple[str, ...] = ()
    import_module: str | None = None
    pip_extra: str | None = None


def env_value(name: str) -> str | None:
    raw = os.getenv(name)
    if raw:
        return raw.strip()
    file_var = os.getenv(f"{name}_FILE")
    if not file_var:
        return None
    try:
        return Path(file_var).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _module_installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _preflight_local_cli(spec: BackendSpec, cli: str) -> PreflightResult:
    checks: list[PreflightCheck] = []
    ready = True
    if not shutil.which(cli):
        checks.append(PreflightCheck(f"{cli}_cli", False, f"{cli} not found on PATH"))
        ready = False
    else:
        checks.append(PreflightCheck(f"{cli}_cli", True, f"{cli} found"))
        try:
            proc = subprocess.run(
                [cli, "info"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(PreflightCheck(f"{cli}_daemon", False, str(exc)))
            ready = False
        else:
            if proc.returncode == 0:
                checks.append(PreflightCheck(f"{cli}_daemon", True, f"{cli} daemon reachable"))
            else:
                checks.append(
                    PreflightCheck(
                        f"{cli}_daemon",
                        False,
                        f"{cli} daemon not reachable — is it running?",
                    )
                )
                ready = False
    return PreflightResult(spec.id, ready=ready, checks=tuple(checks))


def _preflight_cloud(spec: BackendSpec) -> PreflightResult:
    checks: list[PreflightCheck] = []
    configured = True
    if spec.import_module:
        if _module_installed(spec.import_module):
            checks.append(PreflightCheck("sdk", True, f"{spec.import_module} SDK installed"))
        else:
            hint = f"pip install {spec.import_module}"
            checks.append(PreflightCheck("sdk", False, f"SDK missing — {hint}"))
            configured = False
    for var in spec.env_vars:
        if env_value(var):
            checks.append(PreflightCheck(var, True, "set"))
        else:
            checks.append(PreflightCheck(var, False, f"Set {var} in backend/.env"))
            configured = False

    if spec.id in _IMPLEMENTED_CLOUD and configured:
        if spec.id == "daytona":
            from app.services.daytona_environment import ping_daytona_api

            ok, detail = ping_daytona_api()
            checks.append(PreflightCheck("api", ok, detail))
            if not ok:
                configured = False
        if configured:
            checks.append(
                PreflightCheck("execution", True, "Adapter ready — sandbox runs enabled")
            )
            return PreflightResult(spec.id, ready=True, checks=tuple(checks))

    note = "Trial execution not implemented yet (Phase 1)."
    checks.append(
        PreflightCheck(
            "execution",
            False,
            note if configured else f"{note} Fix SDK/credentials/API first.",
        )
    )
    return PreflightResult(spec.id, ready=False, checks=tuple(checks))


BACKEND_SPECS: dict[str, BackendSpec] = {
    "docker": BackendSpec("docker", "Docker · local container", "local"),
    "podman": BackendSpec("podman", "Podman · local container", "local"),
    "daytona": BackendSpec(
        "daytona",
        "Daytona · cloud sandbox",
        "cloud",
        env_vars=("DAYTONA_API_KEY",),
        import_module="daytona",
        pip_extra="daytona",
    ),
    "e2b": BackendSpec(
        "e2b",
        "E2B · cloud sandbox",
        "cloud",
        env_vars=("E2B_API_KEY",),
        import_module="e2b",
        pip_extra="e2b",
    ),
    "modal": BackendSpec(
        "modal",
        "Modal · serverless sandbox",
        "cloud",
        env_vars=("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
        import_module="modal",
        pip_extra="modal",
    ),
    "runloop": BackendSpec(
        "runloop",
        "Runloop · agent sandbox",
        "cloud",
        env_vars=("RUNLOOP_API_KEY",),
        import_module="runloop_api_client",
        pip_extra="runloop",
    ),
    "novita": BackendSpec(
        "novita",
        "Novita · AI sandbox",
        "cloud",
        env_vars=("NOVITA_API_KEY",),
        import_module="novita",
        pip_extra="novita",
    ),
}


def normalize_backend(name: str) -> str:
    key = (name or DEFAULT_BACKEND).strip().lower()
    if key not in BACKEND_SPECS:
        known = ", ".join(sorted(BACKEND_SPECS))
        raise ValueError(f"Unsupported backend {name!r}. Available: {known}.")
    return key


def backend_spec(name: str) -> BackendSpec:
    return BACKEND_SPECS[normalize_backend(name)]


def preflight_backend(name: str) -> PreflightResult:
    spec = backend_spec(name)
    if spec.kind == "local":
        cli = "docker" if spec.id == "docker" else "podman"
        return _preflight_local_cli(spec, cli)
    return _preflight_cloud(spec)


def list_backend_entries() -> list[BackendEntry]:
    from app.services.environment import available_backends

    entries: list[BackendEntry] = []
    for backend_id in available_backends():
        spec = BACKEND_SPECS.get(backend_id)
        if spec is None:
            # A backend registered through the documented extension point
            # (register_environment) that this catalog has no spec for. Indexing
            # BACKEND_SPECS directly meant any third-party backend turned the
            # whole listing into a KeyError. It is registered, so list it; there
            # is no preflight to run, so it reports ready.
            entries.append(
                BackendEntry(id=backend_id, label=backend_id, available=True, ready=True)
            )
            continue
        pf = preflight_backend(backend_id)
        entries.append(
            BackendEntry(
                id=backend_id,
                label=spec.label,
                available=True,
                ready=pf.ready,
            )
        )
    return entries


def assert_backend_ready(name: str) -> PreflightResult:
    backend = normalize_backend(name)
    return preflight_backend(backend)


def backend_not_ready_message(name: str) -> str | None:
    result = assert_backend_ready(name)
    if result.ready:
        return None
    parts = [c.detail for c in result.checks if not c.ok]
    return "; ".join(parts) if parts else "Backend is not ready to run trials."


def _load_daytona() -> type:
    from app.services.daytona_environment import DaytonaEnvironment

    return DaytonaEnvironment


def register_all_backends() -> None:
    """Register cloud stubs + real Daytona adapter."""
    for spec in BACKEND_SPECS.values():
        if spec.kind != "cloud" or spec.id in _IMPLEMENTED_CLOUD:
            continue
        cls = type(
            f"{spec.id.title()}Environment",
            (PlannedCloudEnvironment,),
            {"PROVIDER_ID": spec.id},
        )
        register_environment(spec.id, cls)

    # Lazy: this module is imported from environment.py's module body, and
    # daytona_environment imports environment.py in turn. Importing the class
    # here closes that loop — see register_environment_lazy.
    register_environment_lazy("daytona", _load_daytona)


register_all_backends()
