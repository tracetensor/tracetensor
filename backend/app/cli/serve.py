"""
`tracetensor serve` — start the full platform: REST API + web dashboard + an
embedded worker, in one process. This is the "team / hosted" door; runs you
start here are visible in the browser and via `tracetensor run --server`.
"""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.text import Text

from app.cli import console as ui


def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8000, "-p", "--port", help="Port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev)."),
) -> None:
    """Start the API + web dashboard (+ embedded worker)."""
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover
        ui.error("uvicorn isn't installed. `pip install -e backend`.")
        raise typer.Exit(1) from e

    url = f"http://{host}:{port}"
    body = Text()
    body.append("dashboard  ", style="key")
    body.append(url, style="brand")
    body.append("\n")
    body.append("API        ", style="key")
    body.append(f"{url}/v1", style="val")
    body.append("\n")
    body.append("health     ", style="key")
    body.append(f"{url}/api/health", style="muted")
    ui.console.print()
    ui.console.print(
        Panel(
            body,
            title="[brand]◆ TraceTensor[/]",
            border_style="accent",
            expand=False,
            padding=(0, 2),
        )
    )
    ui.hint("Ctrl-C to stop.\n")

    uvicorn.run("app.main:app", host=host, port=port, reload=reload, log_level="warning")
