"""`tracetensor backends` — list and preflight execution backends."""

from __future__ import annotations

import typer

import app.core.config  # noqa: F401 — load backend/.env before preflight reads env vars
from app.cli import console as ui
from app.services.backend_catalog import (
    assert_backend_ready,
    backend_spec,
    list_backend_entries,
    normalize_backend,
)
from app.services.environment import available_backends

backends_app = typer.Typer(
    help="List and preflight execution backends (local Docker + cloud sandboxes).",
    no_args_is_help=True,
)


@backends_app.command("list")
def list_backends(json_out: bool = typer.Option(False, "--json", help="Machine-readable.")) -> None:
    """Show registered backends and whether each is ready to run trials."""
    entries = list_backend_entries()
    if json_out:
        import json

        ui.console.print(json.dumps([e.model_dump() for e in entries], indent=2))
        return
    ui.console.print()
    ui.console.print("[bold]Execution backends[/]")
    for entry in entries:
        mark = "[ok]ready[/]" if entry.ready else "[warn]not ready[/]"
        ui.console.print(f"  {entry.id:10} {mark:16} {entry.label}")
    ui.console.print()
    ui.hint("Preflight one backend: tracetensor backends preflight daytona")


@backends_app.command("preflight")
def preflight(
    backend: str = typer.Argument(..., help="Backend id (e.g. docker, daytona, modal)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable."),
) -> None:
    """Check whether a backend can run trials (CLI, daemon, SDK, credentials)."""
    try:
        name = normalize_backend(backend)
    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(2) from exc

    result = assert_backend_ready(name)
    spec = backend_spec(name)
    if json_out:
        import json

        payload = {
            "backend": result.backend,
            "ready": result.ready,
            "label": spec.label,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in result.checks],
        }
        ui.console.print(json.dumps(payload, indent=2))
        raise typer.Exit(0 if result.ready else 1)

    ui.console.print()
    ui.console.print(f"[bold]Preflight · {spec.label}[/] ({name})")
    for check in result.checks:
        mark = "✓" if check.ok else "✗"
        style = "ok" if check.ok else "warn"
        ui.console.print(f"  [{style}]{mark}[/] {check.name}: {check.detail}")
    ui.console.print()
    if result.ready:
        ui.console.print(f"[ok]✓[/] {name} is ready to run trials.")
        raise typer.Exit(0)
    ui.error(f"{name} is not ready to run trials.")
    if spec.kind == "cloud":
        ui.hint("Cloud execution lands in Phase 1 — preflight checks credentials/SDK only.")
    raise typer.Exit(1)


def ensure_backend_ready(backend: str) -> str:
    """Normalize + preflight; exit 2 with messages when not ready."""
    try:
        name = normalize_backend(backend)
    except ValueError as exc:
        ui.error(str(exc))
        ui.hint(f"Registered: {', '.join(available_backends())}")
        raise typer.Exit(2) from exc

    result = assert_backend_ready(name)
    if result.ready:
        return name
    for check in result.checks:
        if not check.ok:
            ui.error(f"{check.name}: {check.detail}")
    raise typer.Exit(2)
