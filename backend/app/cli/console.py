"""
The CLI's visual language — one place that owns colour, the wordmark, and the
reusable panels/tables, so every command looks like the same product.

Design intent mirrors the web dashboard: dark, minimal, monochrome + a single
cool accent, semantic colour only where it carries meaning (pass green, fail
red). No gaudy ASCII art — a quiet, premium "instrument" feel.
"""

from __future__ import annotations

from typing import Callable

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from app.models.enums import PhaseStatus
from app.services.cost import cost_enabled

# ---- palette -------------------------------------------------------------
ACCENT = "#5ed0f0"  # cool cyan — the brand accent (matches the dashboard)
OK = "#4ade80"  # pass / good
BAD = "#f87171"  # fail / error
WARN = "#fbbf24"  # warning

THEME = Theme(
    {
        "brand": f"bold {ACCENT}",
        "accent": ACCENT,
        "ok": f"bold {OK}",
        "bad": f"bold {BAD}",
        "warn": WARN,
        "muted": "grey58",
        "key": "grey70",
        "val": "bold white",
        "rule": "grey30",
    }
)

# stderr=False for normal output; a dedicated console keeps styling consistent.
console = Console(theme=THEME, highlight=False)
err_console = Console(theme=THEME, stderr=True, highlight=False)

_MARK = "◆"


def banner() -> None:
    """The wordmark — shown on the bare command and in --help."""
    line = Text()
    line.append(f"{_MARK} ", style="brand")
    line.append("TraceTensor", style="brand")
    line.append("   self-hosted agent evaluation", style="muted")
    console.print(line)


def kv_panel(title: str, rows: list[tuple[str, str]], style: str = "accent") -> Panel:
    """A compact key/value panel (used for the run header)."""
    body = Text()
    for i, (k, v) in enumerate(rows):
        if i:
            body.append("\n")
        body.append(f"{k:<12}", style="key")
        body.append(str(v), style="val")
    return Panel(body, title=f"[brand]{title}[/]", border_style=style, expand=False, padding=(0, 2))


def runs_table(runs: list[dict]) -> Table:
    """Per-trial results — mark, reward, duration, steps, tokens, cost."""
    t = Table(box=None, pad_edge=False, expand=False)
    t.add_column("Trial", style="muted", justify="right")
    t.add_column("Result")
    t.add_column("Reward", justify="right")
    t.add_column("Time", justify="right", style="muted")
    t.add_column("Steps", justify="right", style="muted")
    t.add_column("Tokens", justify="right", style="muted")
    if cost_enabled():
        t.add_column("Cost", justify="right", style="muted")
    for tr in runs:
        ok = tr.get("passed") is True
        mark = Text("✓ pass", style="ok") if ok else Text("✗ fail", style="bad")
        reward = "—" if tr.get("reward") is None else f"{tr['reward']:.3f}"
        dur = "—" if tr.get("duration_s") is None else f"{tr['duration_s']:.1f}s"
        # Prefer explicit token fields; else pull from nested trajectory summary.
        in_tok = tr.get("input_tokens")
        out_tok = tr.get("output_tokens")
        cost = tr.get("cost_usd")
        if in_tok is None and isinstance(tr.get("trajectory"), dict):
            from app.services.vault import usage_from_trajectory

            u = usage_from_trajectory(tr.get("trajectory"))
            if u.calls:
                in_tok, out_tok, cost = u.input_tokens, u.output_tokens, u.cost_usd
        if in_tok is None and out_tok is None:
            tok = "—"
        else:
            tok = f"{in_tok or 0}/{out_tok or 0}"
        if cost is None:
            cost_s = "—"
        elif cost < 0.01:
            cost_s = f"${cost:.4f}"
        else:
            cost_s = f"${cost:.3f}"
        # Annotated as the union rich actually accepts: the row mixes plain
        # strings with a styled Text mark, which infers as list[object] and isn't
        # assignable to add_row's parameter.
        row: list[RenderableType] = [
            f"#{tr['n'] + 1}" if isinstance(tr.get("n"), int) else f"#{tr.get('n', '?')}",
            mark,
            reward,
            dur,
            str(tr.get("steps", "")),
            tok,
        ]
        if cost_enabled():
            row.append(cost_s)
        t.add_row(*row)
    return t


# Internal name; product output uses runs_table.
trials_table = runs_table


def summary_panel(
    passed: int,
    total: int,
    mean_reward: float | None,
    wall_s: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> Panel:
    """The headline result — pass rate + mean reward + optional usage."""
    rate = passed / total if total else 0.0
    tone = "ok" if rate == 1.0 else ("bad" if rate == 0.0 else "warn")
    body = Text()
    body.append("pass rate  ", style="key")
    body.append(f"{passed}/{total}", style=tone)
    body.append(f"  ({rate * 100:.0f}%)\n", style=tone)
    body.append("mean reward", style="key")
    body.append(" ", style="key")
    body.append("—" if mean_reward is None else f"{mean_reward:.3f}", style="val")
    body.append("\n")
    body.append("wall time  ", style="key")
    body.append(f"{wall_s:.1f}s", style="muted")
    if input_tokens is not None or output_tokens is not None:
        body.append("\n")
        body.append("usage      ", style="key")
        in_s = "—" if input_tokens is None else str(input_tokens)
        out_s = "—" if output_tokens is None else str(output_tokens)
        usage = f"{in_s} in · {out_s} out"
        if cost_enabled() and cost_usd is not None:
            if cost_usd < 0.01:
                cost_s = f"${cost_usd:.4f}"
            else:
                cost_s = f"${cost_usd:.3f}"
            usage = f"{usage} · {cost_s}  (estimate)"
        body.append(usage, style="muted")
    return Panel(body, title="[brand]result[/]", border_style=tone, expand=False, padding=(0, 2))


def error(msg: str) -> None:
    err_console.print(f"[bad]✗[/] {msg}")


def hint(msg: str) -> None:
    console.print(f"[muted]{msg}[/]")


def warn(msg: str) -> None:
    console.print(f"[warn]⚠[/] {msg}")


_PHASE_LABEL = {
    "setup": "setup",
    "agent": "agent",
    "verify": "verifier",
    "score": "score",
}


def make_phase_tracker(
    trial_num: int | None = None,
    agent_timeout: float | None = None,
) -> tuple[Callable[[dict], None], Callable[[], str]]:
    """Build an on_event hook + getter for live CLI phase text."""
    prefix = f"trial #{trial_num + 1} · " if trial_num is not None else ""
    state = {"text": f"{prefix}starting…"}

    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "phase":
            phase = str(event.get("phase") or "?")
            status = str(event.get("status") or "")
            label = _PHASE_LABEL.get(phase, phase)
            detail = event.get("detail")
            if status == PhaseStatus.RUNNING:
                extra = ""
                if phase == "agent" and agent_timeout:
                    extra = f" (up to {int(agent_timeout)}s)"
                elif detail:
                    extra = f" ({detail})"
                state["text"] = f"{prefix}{label}{extra}…"
            elif status == "done":
                state["text"] = f"{prefix}{label} ✓"
            elif status == "error":
                state["text"] = f"{prefix}{label} failed"
        elif kind == "step" and event.get("phase") == "agent":
            cmd = str(event.get("command") or "")[:48]
            state["text"] = f"{prefix}agent · {cmd}…"

    def current() -> str:
        return state["text"]

    return on_event, current
