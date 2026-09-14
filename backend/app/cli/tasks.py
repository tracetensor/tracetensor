"""
`tracetensor tasks` — inspect a task before you run it.

`validate` gives the same ready / not-ready verdict the platform uses at intake,
so you catch a malformed task (missing tests, no Dockerfile, …) locally.
`init` scaffolds a task folder.

Tasks use the standard task folder layout:
instruction.md, task.toml, environment/, tests/, solution/ (optional).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from app.cli import console as ui

tasks_app = typer.Typer(
    help="Inspect and validate tasks.",
    no_args_is_help=True,
)


@tasks_app.command("init")
def init_cmd(
    path: Path = typer.Argument(
        Path("my-task"),
        help="Directory to create (standard task layout).",
    ),
    name: Optional[str] = typer.Option(
        None, "--name", "-n", help="[task].name (default: directory name)."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Scaffold a new task folder."""
    dest = path.resolve()
    task_name = name or dest.name
    if dest.exists() and any(dest.iterdir()) and not force:
        ui.error(f"{dest} is not empty — pass --force to overwrite scaffold files.")
        raise typer.Exit(2)

    (dest / "environment").mkdir(parents=True, exist_ok=True)
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    (dest / "solution").mkdir(parents=True, exist_ok=True)

    files = {
        "instruction.md": (f"# {task_name}\n\nDescribe what the agent should do.\n"),
        "task.toml": (
            'schema_version = "1.3"\n\n'
            "[task]\n"
            f'name = "{task_name}"\n'
            'description = "TODO: one-line description"\n'
            "keywords = []\n\n"
            "[metadata]\n"
            'category = "programming"\n\n'
            "[environment]\n"
            'docker_image = "python:3.11-slim"\n'
            'workdir = "/app"\n'
            'network_mode = "no-network"\n'
            '# platform = "linux/amd64"  # Apple Silicon: uncomment for amd64-only images\n\n'
            "[agent]\n"
            "timeout_sec = 120.0\n\n"
            "[verifier]\n"
            "timeout_sec = 60.0\n"
        ),
        "environment/Dockerfile": ("FROM python:3.11-slim\nWORKDIR /app\n"),
        "tests/test.sh": (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "# Write a score the harness can read (0.0 .. 1.0).\n"
            "mkdir -p /logs/verifier\n"
            "echo 0 > /logs/verifier/reward.txt\n"
            'echo "TODO: implement checks; echo 1 on success"\n'
            "exit 1\n"
        ),
        "solution/solve.sh": (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "# Optional reference solution for `tracetensor run -a oracle`.\n"
            "# Env from solution/.env or [solution].env in task.toml is injected.\n"
            'echo "TODO: implement reference solution"\n'
        ),
    }
    for rel, content in files.items():
        target = dest / rel
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if rel.endswith(".sh"):
            target.chmod(target.stat().st_mode | 0o111)

    ui.banner()
    ui.console.print(f"[ok]✓ scaffolded[/] [val]{dest}[/]")
    ui.hint("Next: edit instruction.md + tests/test.sh, then:")
    ui.hint(f"  tracetensor tasks validate {dest}")
    ui.hint(f"  tracetensor run {dest} -a oracle")


@tasks_app.command()
def validate(
    path: Path = typer.Argument(..., help="Path to a task directory."),
) -> None:
    """Check whether a task is ready to run (same rules as the platform)."""
    from app.services.task_validator import STATUS_READY, validate_task

    if not path.exists():
        ui.error(f"No such path: {path}")
        raise typer.Exit(2)

    if not (path / "task.toml").exists() and not (path / "instruction.md").exists():
        ui.error(
            f"{path} doesn't look like a task "
            "(no task.toml / instruction.md). Point at a task directory."
        )
        raise typer.Exit(2)

    res = validate_task(path)
    checks = [
        ("instruction.md", res.has_instruction),
        ("task.toml", res.has_task_toml),
        ("environment (Dockerfile / image)", res.has_dockerfile or res.has_docker_image),
        ("tests/test.sh", res.has_test_script),
        ("solution/solve.sh (optional)", res.has_solution),
    ]
    t = Table(box=None, pad_edge=False, expand=False)
    t.add_column("", justify="left")
    t.add_column("", justify="left")
    for label, ok in checks:
        mark = "[ok]✓[/]" if ok else "[bad]✗[/]"
        t.add_row(mark, f"[{'val' if ok else 'muted'}]{label}[/]")
    ui.console.print()
    ui.console.print(t)

    ready = res.status == STATUS_READY
    ui.console.print()
    if ready:
        ui.console.print(f"[ok]✓ ready[/] — [muted]{path}[/]")
    else:
        ui.console.print(f"[bad]✗ not ready[/] — [muted]{path}[/]")
    for e in res.errors:
        ui.console.print(f"  [bad]•[/] {e}")
    for w in res.warnings:
        ui.console.print(f"  [warn]•[/] {w}")
    for note in getattr(res, "host_notes", []):
        ui.console.print(f"  [warn]•[/] {note}")
    tier = getattr(res, "local_tier", None)
    if tier:
        ui.console.print(f"  [muted]local tier:[/] {tier}")
    ui.console.print()
    raise typer.Exit(0 if ready else 1)


@tasks_app.command("pull")
def pull_cmd(
    package: str = typer.Argument(..., help="Harbor Hub task: org/name or org/name@latest"),
    out: Path = typer.Option(Path("tasks"), "-o", "--out", help="Output directory."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Re-download even if cached."),
    cache: bool = typer.Option(
        False,
        "--cache",
        help="Store under ~/.cache/tracetensor/registry instead of export layout.",
    ),
) -> None:
    """Download a public task from Harbor Hub (direct registry API)."""
    from app.services.harbor_registry import (
        HarborRegistryClient,
        RegistryAuthError,
        RegistryError,
        RegistryNotFoundError,
    )
    from app.services.task_validator import STATUS_READY, validate_task

    client = HarborRegistryClient()
    try:
        result = client.download_task(
            package,
            output_dir=out,
            export=not cache,
            overwrite=overwrite,
        )
    except RegistryNotFoundError as e:
        ui.error(str(e))
        raise typer.Exit(2) from e
    except RegistryAuthError as e:
        ui.error(f"{e} Set TRACETENSOR_REGISTRY_TOKEN for private packages.")
        raise typer.Exit(2) from e
    except RegistryError as e:
        ui.error(str(e))
        raise typer.Exit(1) from e

    label = "cached" if result.cached else "downloaded"
    ui.console.print(f"[ok]✓[/] {label} → [val]{result.path}[/]")

    res = validate_task(result.path)
    if res.status == STATUS_READY:
        ui.console.print("[ok]✓ ready[/] — task validates for local run")
    else:
        ui.console.print("[warn]⚠[/] downloaded but validation reported issues:")
        for e in res.errors:
            ui.console.print(f"  [bad]•[/] {e}")
        for w in res.warnings:
            ui.console.print(f"  [warn]•[/] {w}")
    ui.hint(f"Run: tracetensor run {result.path} -a oracle")
    raise typer.Exit(0 if res.status == STATUS_READY else 1)
