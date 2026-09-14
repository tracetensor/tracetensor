"""
`tracetensor vault` — local (and optional --server) browse / show / export of runs.

Phase 1: local `runs/` + server job history. Remote share (`push`) is Phase 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from app.cli import console as ui
from app.services import diagnose as diagnose_svc
from app.services import vault as vault_svc
from app.services.cost import cost_enabled

vault_app = typer.Typer(
    help="Browse and export saved trial results (local Vault).",
    no_args_is_help=True,
)


@vault_app.command("list")
def list_cmd(
    runs: Path = typer.Option(Path("runs"), "--runs", help="Local runs directory."),
    server: Optional[str] = typer.Option(
        None, "--server", help="List jobs from a running `tracetensor serve`."
    ),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="TRACETENSOR_TOKEN", help="API token if the server is secured."
    ),
) -> None:
    """List saved runs (local `runs/` and/or a --server)."""
    ui.banner()
    if server:
        from app.cli import client

        try:
            jobs = client.list_jobs(server.rstrip("/"), token)
        except client.ServerError as e:
            ui.error(str(e))
            raise typer.Exit(1) from e
        if not jobs:
            ui.hint("No jobs on the server yet.")
            return
        t = Table(box=None, pad_edge=False, expand=False)
        t.add_column("Job", style="muted")
        t.add_column("Task")
        t.add_column("Agent")
        t.add_column("Pass", justify="right")
        t.add_column("Time", justify="right", style="muted")
        t.add_column("Tokens", justify="right", style="muted")
        if cost_enabled():
            t.add_column("Cost", justify="right", style="muted")
        for j in jobs:
            if j.get("dataset_run_id"):
                continue
            rate = j.get("pass_rate")
            rate_s = "—" if rate is None else f"{round(rate * 100)}%"
            dur = j.get("duration_s")
            dur_s = "—" if dur is None else f"{dur:.1f}s"
            if j.get("input_tokens") is not None or j.get("output_tokens") is not None:
                tok = (
                    f"{vault_svc.format_tokens(j.get('input_tokens'))}/"
                    f"{vault_svc.format_tokens(j.get('output_tokens'))}"
                )
            else:
                tok = "—"
            row = [
                str(j.get("id", ""))[:8],
                str(j.get("task_name") or j.get("task_id") or "")[:28],
                f"{j.get('agent')}" + (f" · {j['model']}" if j.get("model") else ""),
                rate_s,
                dur_s,
                tok,
            ]
            if cost_enabled():
                row.append(vault_svc.format_cost(j.get("cost_usd")))
            t.add_row(*row)
        ui.console.print(t)
        return

    rows = vault_svc.list_local_runs(runs)
    if not rows:
        ui.hint(f"No local runs under {runs.resolve()} — run a task first.")
        return
    t = Table(box=None, pad_edge=False, expand=False)
    t.add_column("Id", style="muted")
    t.add_column("Task")
    t.add_column("Agent")
    t.add_column("Pass", justify="right")
    t.add_column("Time", justify="right", style="muted")
    t.add_column("Tokens", justify="right", style="muted")
    if cost_enabled():
        t.add_column("Cost", justify="right", style="muted")
    for r in rows:
        rate = r.get("pass_rate")
        rate_s = "—" if rate is None else f"{round(rate * 100)}%"
        dur = r.get("duration_s")
        dur_s = "—" if dur is None else f"{dur:.1f}s"
        if r.get("input_tokens") is not None:
            tok = (
                f"{vault_svc.format_tokens(r.get('input_tokens'))}/"
                f"{vault_svc.format_tokens(r.get('output_tokens'))}"
            )
        else:
            tok = "—"
        row = [
            str(r["id"])[:40],
            str(r.get("task") or "")[:28],
            f"{r.get('agent')}" + (f" · {r['model']}" if r.get("model") else ""),
            rate_s,
            dur_s,
            tok,
        ]
        if cost_enabled():
            row.append(vault_svc.format_cost(r.get("cost_usd")))
        t.add_row(*row)
    ui.console.print(t)
    ui.hint(f"local Vault · {runs.resolve()}")


@vault_app.command("show")
def show_cmd(
    run_id: str = typer.Argument(..., help="Local run directory name/path, or server job UUID."),
    runs: Path = typer.Option(Path("runs"), "--runs", help="Local runs directory."),
    server: Optional[str] = typer.Option(
        None, "--server", help="Fetch the job from a running server."
    ),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="TRACETENSOR_TOKEN", help="API token if the server is secured."
    ),
) -> None:
    """Show one saved run (summary + per-trial table)."""
    ui.banner()
    if server:
        from app.cli import client

        try:
            job = client.get_job(server.rstrip("/"), run_id, token)
        except client.ServerError as e:
            ui.error(str(e))
            raise typer.Exit(1) from e
        trials_raw = job.get("trials") or []
        usage = vault_svc.rollup_usage(trials_raw)
        rows = [
            ("task", str(job.get("task_name") or job.get("task_id"))),
            ("agent", job.get("agent", "") + (f" · {job['model']}" if job.get("model") else "")),
            ("status", str(job.get("status"))),
            (
                "pass",
                f"{job.get('trials_passed', 0)}/{job.get('n_trials', 0)}"
                + (
                    f"  ({job['pass_rate'] * 100:.0f}%)" if job.get("pass_rate") is not None else ""
                ),
            ),
            (
                "usage",
                vault_svc.format_usage_line(
                    usage.input_tokens, usage.output_tokens, usage.cost_usd
                ),
            ),
        ]
        ui.console.print(ui.kv_panel("vault", rows))
        trial_rows = []
        for t in trials_raw:
            u = vault_svc.usage_from_trajectory(t.get("trajectory"))
            trial_rows.append(
                {
                    "n": t.get("trial_num", 0),
                    "passed": t.get("passed"),
                    "reward": t.get("reward"),
                    "duration_s": t.get("duration_s"),
                    "steps": len((t.get("trajectory") or {}).get("steps", []) or []),
                    "input_tokens": u.input_tokens if u.calls else None,
                    "output_tokens": u.output_tokens if u.calls else None,
                    "cost_usd": u.cost_usd,
                }
            )
        ui.console.print()
        ui.console.print(ui.runs_table(trial_rows))
        return

    try:
        run_dir = vault_svc.resolve_local_run(runs, run_id)
        data = vault_svc.load_local_run(run_dir)
    except FileNotFoundError as e:
        ui.error(str(e))
        raise typer.Exit(2) from e
    # A different shape from the server branch's UsageRollup — this one is the
    # raw dict as it was written to result.json — so it gets its own name.
    local_usage = data.get("usage") or {}
    agent_s = str(data.get("agent") or "")
    if data.get("model"):
        agent_s += f" · {data['model']}"
    rows = [
        ("id", data.get("id", "")),
        ("task", str(data.get("task"))),
        ("agent", agent_s),
        ("pass", f"{data.get('passed', 0)}/{data.get('n_trials', 0)}"),
        ("wall", f"{data.get('wall_s')}s" if data.get("wall_s") is not None else "—"),
        (
            "usage",
            vault_svc.format_usage_line(
                local_usage.get("input_tokens"),
                local_usage.get("output_tokens"),
                local_usage.get("cost_usd"),
            ),
        ),
        ("path", data.get("path", "")),
    ]
    ui.console.print(ui.kv_panel("vault", rows))
    trial_rows = []
    for t in data.get("trials") or []:
        u = vault_svc.usage_from_trajectory(t.get("trajectory"))
        trial_rows.append(
            {
                "n": t.get("n", t.get("trial_num", 0)),
                "passed": t.get("passed"),
                "reward": t.get("reward"),
                "duration_s": t.get("duration_s"),
                "steps": t.get("steps")
                if t.get("steps") is not None
                else len((t.get("trajectory") or {}).get("steps", []) or []),
                "input_tokens": u.input_tokens if u.calls else None,
                "output_tokens": u.output_tokens if u.calls else None,
                "cost_usd": u.cost_usd,
            }
        )
    ui.console.print()
    ui.console.print(ui.runs_table(trial_rows))


@vault_app.command("export")
def export_cmd(
    run_id: str = typer.Argument(..., help="Local run directory name/path, or server job UUID."),
    out: Path = typer.Option(Path("vault-export"), "-o", "--out", help="Output directory."),
    runs: Path = typer.Option(Path("runs"), "--runs", help="Local runs directory."),
    server: Optional[str] = typer.Option(
        None, "--server", help="Export a job from a running server."
    ),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="TRACETENSOR_TOKEN", help="API token if the server is secured."
    ),
) -> None:
    """Write a portable archive of one run (JSON)."""
    ui.banner()
    if server:
        from app.cli import client

        try:
            job = client.get_job(server.rstrip("/"), run_id, token)
            path = vault_svc.export_job_dict(job, out, run_id)
        except (client.ServerError, OSError) as e:
            ui.error(str(e))
            raise typer.Exit(1) from e
        ui.hint(f"exported → {path}")
        return
    try:
        run_dir = vault_svc.resolve_local_run(runs, run_id)
        dest = vault_svc.export_local_run(run_dir, out)
    except (FileNotFoundError, OSError) as e:
        ui.error(str(e))
        raise typer.Exit(2) from e
    ui.hint(f"exported → {dest}")


@vault_app.command("diagnose")
def diagnose_cmd(
    run_id: Optional[str] = typer.Argument(
        None, help="Local run directory name/path. Omit to diagnose every local run."
    ),
    runs: Path = typer.Option(Path("runs"), "--runs", help="Local runs directory."),
    max_steps: Optional[int] = typer.Option(
        None,
        "--max-steps",
        help="The step budget the run used (LLM_AGENT_MAX_STEPS at run time). "
        "Makes budget-exhaustion definitive instead of inferred.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Persist each trial's failure_analysis back into result.json.",
    ),
    use_llm: bool = typer.Option(
        False,
        "--llm",
        help="Also run the LLM extractor on each trial (COSTS a model call per "
        "trial — needs a provider API key; see DIAGNOSE_MODEL).",
    ),
    llm_model: Optional[str] = typer.Option(
        None, "--llm-model", help="provider/model for --llm (default openai/gpt-4.1-mini)."
    ),
) -> None:
    """Explain WHY trials failed (rules are free; --llm adds a paid extraction pass)."""
    ui.banner()
    if run_id:
        try:
            dirs = [vault_svc.resolve_local_run(runs, run_id)]
        except FileNotFoundError as e:
            ui.error(str(e))
            raise typer.Exit(2) from e
    else:
        dirs = [Path(r["path"]) for r in vault_svc.list_local_runs(runs)]
        if not dirs:
            ui.hint(f"No runs under {runs}/.")
            return

    for run_dir in dirs:
        try:
            data = vault_svc.load_local_run(run_dir)
        except FileNotFoundError as e:
            ui.error(str(e))
            continue
        if use_llm:
            from app.services import diagnose_llm

            # Best-effort instruction lookup: `tracetensor run` names runs
            # after the task path, so try tasks/<name>/instruction.md from cwd.
            instruction = ""
            task_name = str(data.get("task") or "")
            for base in (Path("tasks"), Path("../tasks")):
                cand = base / task_name / "instruction.md"
                if cand.is_file():
                    instruction = cand.read_text(encoding="utf-8", errors="replace")
                    break
            analyses = [
                diagnose_llm.diagnose_trial_full(
                    t, instruction, model_spec=llm_model, max_steps=max_steps
                )
                for t in (data.get("trials") or [])
                if isinstance(t, dict)
            ]
        else:
            analyses = diagnose_svc.diagnose_result(data, max_steps=max_steps)
        agent_s = str(data.get("agent") or "")
        if data.get("model"):
            agent_s += f" · {data['model']}"
        ui.console.print()
        ui.console.print(
            f"[bold]{run_dir.name}[/bold]  [muted]{data.get('task')}  {agent_s}[/muted]"
        )
        trials = data.get("trials") or []
        for t, fa in zip(trials, analyses):
            n = t.get("n", t.get("trial_num", 0))
            mark = "[ok]pass[/ok]" if t.get("passed") else "[bad]FAIL[/bad]"
            if not fa["occurrences"]:
                ui.console.print(f"  trial {n}  {mark}  [muted]no findings[/muted]")
                continue
            ui.console.print(f"  trial {n}  {mark}")
            for occ in fa["occurrences"]:
                ev = occ["evidence_steps"]
                ev_s = f"  [muted]steps {ev[0]}–{ev[-1]}[/muted]" if ev else ""
                det = "  [muted](llm)[/muted]" if occ.get("detector") == "llm" else ""
                ui.console.print(
                    f"    [warn]{occ['failure_class']}[/warn]  {occ['title']}{ev_s}{det}"
                )
                ui.console.print(f"      [muted]{occ['rationale']}[/muted]")
            if fa.get("llm_call"):
                cost = vault_svc.format_cost((fa["llm_call"] or {}).get("cost_usd"))
                ui.console.print(
                    f"      [muted]llm: {fa['llm_call'].get('provider')}/"
                    f"{fa['llm_call'].get('model')}  cost {cost}[/muted]"
                )
            if fa.get("llm_error"):
                ui.console.print(f"      [warn]llm: {fa['llm_error']}[/warn]")
        if write:
            for t, fa in zip(trials, analyses):
                traj = t.setdefault("trajectory", {})
                traj["failure_analysis"] = fa
            import json as _json

            (run_dir / "result.json").write_text(
                _json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
            ui.hint(f"failure_analysis written → {run_dir / 'result.json'}")
