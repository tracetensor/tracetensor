"""
`tracetensor` — the command-line interface.

    tracetensor run <task> -a <agent> -m <model> -n <N>   run trials + verifier
    tracetensor serve                                     start the API server
    tracetensor tasks init <dir>                          scaffold a new task
    tracetensor tasks validate <task>                     check a task is runnable
    tracetensor dataset run <dir> -a <agent>              run all tasks in a dataset
    tracetensor vault list                                browse saved results
    tracetensor vault show <run>                          inspect a specific run
    tracetensor vault export <run> -o <path>              export run artifacts
    tracetensor version                                   show installed version

Local by default (no server needed); the same backend the dashboard uses, so a
run here shows up there too. This module exposes `app`, the entry point wired in
pyproject.toml ([project.scripts] tracetensor = "app.cli.main:app").
"""

from __future__ import annotations

import typer

from app.cli import console as ui
from app.cli.backends import backends_app
from app.cli.dataset import dataset_app
from app.cli.init_env import init_env_cmd as _init
from app.cli.run import run as _run
from app.cli.serve import serve as _serve
from app.cli.tasks import tasks_app
from app.cli.vault import vault_app

app = typer.Typer(
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Self-hosted platform for evaluating coding agents — CLI + web dashboard.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

# Top-level commands.
app.command("init")(_init)
app.command("run")(_run)
app.command("serve")(_serve)
# Sub-app: `tracetensor tasks validate …`
app.add_typer(tasks_app, name="tasks")
# Sub-app: `tracetensor backends list|preflight …`
app.add_typer(backends_app, name="backends")
# Sub-app: `tracetensor dataset run … / pull …`
app.add_typer(dataset_app, name="dataset")
# Sub-app: `tracetensor vault list|show|export …`
app.add_typer(vault_app, name="vault")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("tracetensor")
    except Exception:  # pragma: no cover - source checkout without install
        from app.core.config import settings

        return settings.APP_VERSION


@app.command()
def version() -> None:
    """Show the installed version."""
    ui.banner()
    ui.console.print(f"[muted]version[/] [val]{_version()}[/]")


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    # Bare `tracetensor` prints the wordmark, then Typer shows help (no_args_is_help).
    if ctx.invoked_subcommand is None:
        ui.banner()
        ui.console.print()


if __name__ == "__main__":  # pragma: no cover
    app()
