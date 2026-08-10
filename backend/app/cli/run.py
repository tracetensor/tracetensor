"""
`tracetensor run` — run an agent against a task locally and grade with the verifier.

Local by default (the default mode): builds the task's Docker image, runs the
agent N trials in real sandboxes, runs the verifier on each trial, and
prints a branded summary. No server or database required — this calls the same
core the web platform uses (app.services.trial_runner.run_trial).
"""

from __future__ import annotations

import json
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from app.cli import console as ui
from app.models.enums import JobStatus


def _resolve_task(path: Path) -> Path:
    """Point at a task directory (instruction.md + task.toml + tests/)."""
    if not path.exists():
        ui.error(f"No such path: {path}")
        raise typer.Exit(2)
    if not (path / "task.toml").exists() and not (path / "instruction.md").exists():
        ui.error(
            f"{path} doesn't look like a task (no task.toml / instruction.md). "
            "Point at a task directory."
        )
        raise typer.Exit(2)
    return path


def run(
    path: Path = typer.Argument(..., help="Path to a task directory."),
    agent: str = typer.Option(
        "oracle",
        "-a",
        "--agent",
        help=(
            "Agent: oracle; installed (mini-swe, claude-code, codex); "
            "anthropic/openai/openrouter; or pkg.mod:Class."
        ),
    ),
    model: Optional[str] = typer.Option(None, "-m", "--model", help="Model id (for LLM agents)."),
    n_trials: int = typer.Option(
        1, "-n", "--n-trials", min=1, max=50, help="Number of trials (repeat the same task)."
    ),
    concurrency: int = typer.Option(
        0, "--concurrency", min=0, max=16, help="Parallel trials (0 = auto)."
    ),
    infra_retries: Optional[int] = typer.Option(
        None,
        "--infra-retries",
        min=0,
        max=3,
        help="Retry setup-only infra failures (0=off). Default: TRACETENSOR_INFRA_RETRIES or 1.",
    ),
    platform: Optional[str] = typer.Option(
        None,
        "--platform",
        help="Docker platform override (e.g. linux/amd64). Default: task.toml or arm64 auto.",
    ),
    output: Path = typer.Option(
        Path("runs"), "-o", "--output", help="Where to write run artifacts (local mode)."
    ),
    no_save: bool = typer.Option(False, "--no-save", help="Don't write result files (local mode)."),
    server: Optional[str] = typer.Option(
        None,
        "--server",
        help="Submit to a running `tracetensor serve` (e.g. http://localhost:8000) so the "
        "run lands in the dashboard history + leaderboard and executes on its queue.",
    ),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="TRACETENSOR_TOKEN", help="API token for the server (if secured)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON (for CI/scripts)."),
    verbose: bool = typer.Option(
        False, "--verbose", help="Emit structured JSON logs to stdout (for debugging)."
    ),
    open_dashboard: bool = typer.Option(
        False,
        "--open",
        help="Open the dashboard in your browser (--server only; link prints immediately).",
    ),
) -> None:
    """Run an agent against a task and grade trials (locally, or on a --server)."""
    if verbose:
        from app.core.logging import configure_logging

        configure_logging()
    from app.core.config import settings
    from app.services.agents import AgentConfigError, resolve_agent
    from app.services.run_warnings import run_warnings
    from app.services.task_parser import TaskParseError, parse_task_toml
    from app.services.trial_hints import enrich_trial_result
    from app.services.trial_runner import prebuild, run_trial

    task_dir = _resolve_task(path)
    instruction = ""
    ipath = task_dir / "instruction.md"
    if ipath.exists():
        instruction = ipath.read_text(errors="replace")

    # Timeouts + a friendly name come from task.toml (fall back to defaults).
    name, agent_to, verifier_to = task_dir.name, 120.0, 120.0
    parsed_cfg = None
    tpath = task_dir / "task.toml"
    if tpath.exists():
        try:
            parsed_cfg = parse_task_toml(tpath.read_bytes(), task_dir)
            name = parsed_cfg.name or name
            agent_to = parsed_cfg.agent.timeout_sec or 120.0
            verifier_to = parsed_cfg.verifier.timeout_sec or 120.0
        except TaskParseError as e:
            ui.error(f"Bad task.toml: {e}")
            raise typer.Exit(2) from e

    try:
        resolve_agent(agent, model, settings)
    except AgentConfigError as e:
        ui.error(str(e))
        raise typer.Exit(2) from e

    conc = concurrency or min(4, n_trials)

    if n_trials > 10 and not json_out:
        ui.warn(f"{n_trials} trials will run — confirm cost/time before large batches.")

    if not json_out:
        rows = [
            ("task", name),
            ("agent", agent + (f" · {model}" if model else "")),
            ("trials", f"{n_trials}  (×{conc} parallel)"),
            ("timeouts", f"agent {int(agent_to)}s · verifier {int(verifier_to)}s"),
            ("where", server if server else "local · docker"),
        ]
        ui.console.print()
        ui.console.print(ui.kv_panel("run", rows))
        if parsed_cfg:
            for w in run_warnings(task_dir, parsed_cfg, agent):
                ui.warn(w)

    retry_n = infra_retries if infra_retries is not None else settings.INFRA_RETRIES

    # ---- remote: submit to a running server (lands in its DB + dashboard) ----
    if server:
        remote, wall, job_url = _run_remote(
            server,
            token,
            task_dir,
            agent,
            model,
            n_trials,
            conc,
            json_out,
            open_dashboard=open_dashboard,
        )
        _report(name, agent, model, n_trials, remote, wall, json_out, saved=job_url, link=True)
        raise typer.Exit(0 if any(r.get("passed") for r in remote) else 1)

    # ---- local: build the image once, run trials here (no server needed) -----
    t0 = time.time()
    try:
        if not json_out:
            with ui.console.status("[muted]preparing sandbox (building image)…", spinner="dots"):
                prebuild(task_dir, "docker")
        else:
            prebuild(task_dir, "docker")
    except Exception as e:
        ui.error(f"Could not prepare the sandbox: {e}")
        ui.hint("Is the Docker daemon running?")
        raise typer.Exit(1) from e

    results: list[dict] = [{} for _ in range(n_trials)]
    phase_latest = {"text": "starting…"}
    live_phase = conc <= 1

    def _one(i: int, refresh: Callable[[], None] | None = None) -> dict:
        on_event, phase_get = ui.make_phase_tracker(i if n_trials > 1 else None, agent_to)

        def _tag(event: dict) -> None:
            on_event(event)
            phase_latest["text"] = phase_get()
            if refresh:
                refresh()

        out = run_trial(
            task_dir=task_dir,
            instruction=instruction,
            agent_name=agent,
            model=model,
            backend="docker",
            agent_timeout=agent_to,
            verifier_timeout=verifier_to,
            on_event=_tag,
            max_infra_retries=retry_n,
            platform_override=platform,
        )
        steps = len((out.trajectory or {}).get("steps", []) or [])
        return enrich_trial_result(
            {
                "n": i,
                "status": out.status,
                "passed": out.passed,
                "reward": out.reward,
                "duration_s": out.duration_s,
                "steps": steps,
                "error": out.error,
                "trajectory": out.trajectory,
                "reward_payload": out.reward_payload,
            }
        )

    if json_out:
        for fut_i in _run_all(lambda i: _one(i), n_trials, conc):
            results[fut_i["n"]] = fut_i
    else:
        with Progress(
            SpinnerColumn(style="accent"),
            TextColumn("[muted]{task.fields[phase]}"),
            BarColumn(complete_style="accent", finished_style="ok"),
            TextColumn("[muted]{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=ui.console,
            transient=True,
        ) as prog:
            bar = prog.add_task("trials", total=n_trials, phase=phase_latest["text"])

            def _refresh() -> None:
                prog.update(bar, phase=phase_latest["text"])

            runner = (lambda i: _one(i, _refresh)) if live_phase else _one
            for res in _run_all(runner, n_trials, conc):
                results[res["n"]] = res
                prog.advance(bar)
                prog.update(bar, phase=phase_latest["text"])

    wall = time.time() - t0
    passed = sum(1 for r in results if r.get("passed"))
    rewards = [r["reward"] for r in results if r.get("reward") is not None]
    mean_reward = sum(rewards) / len(rewards) if rewards else None

    saved_to = None
    if not no_save:
        saved_to = str(_save(output, name, agent, model, results, passed, mean_reward, wall))

    _report(name, agent, model, n_trials, results, wall, json_out, saved=saved_to, link=False)
    # Nonzero exit if nothing passed, so CI can gate on it.
    raise typer.Exit(0 if passed > 0 else 1)


def _report(
    name: str,
    agent: str,
    model: str | None,
    n_trials: int,
    results: list,
    wall: float,
    json_out: bool,
    saved: str | None,
    link: bool,
) -> None:
    """Render the results the same way for local and remote runs."""
    from app.services.trial_hints import enrich_trial_result

    results = [enrich_trial_result(dict(r)) for r in results]
    passed = sum(1 for r in results if r.get("passed"))
    rewards = [r["reward"] for r in results if r.get("reward") is not None]
    mean_reward = sum(rewards) / len(rewards) if rewards else None
    if json_out:
        print(
            json.dumps(
                {
                    "task": name,
                    "agent": agent,
                    "model": model,
                    "n_trials": n_trials,
                    "passed": passed,
                    "pass_rate": (passed / n_trials if n_trials else None),
                    "mean_reward": mean_reward,
                    "wall_s": round(wall, 2),
                    "trials": [
                        {
                            k: r.get(k)
                            for k in (
                                "n",
                                "status",
                                "passed",
                                "reward",
                                "duration_s",
                                "steps",
                                "error",
                                "failure_hint",
                            )
                        }
                        for r in results
                    ],
                    ("job_url" if link else "saved_to"): saved,
                },
                indent=2,
            )
        )
        return
    ui.console.print()
    ui.console.print(ui.runs_table(results))
    failed = [r for r in results if not r.get("passed")]
    if failed:
        ui.console.print()
        ui.console.print(Text("Failed trials", style="bad"))
        for r in failed:
            n = r.get("n", "?")
            label = f"#{n + 1}" if isinstance(n, int) else str(n)
            hint = r.get("failure_hint") or r.get("error") or "unknown"
            ui.console.print(f"  [bad]trial {label}[/]  {hint}")
    ui.console.print()
    from app.services.vault import rollup_usage

    usage = rollup_usage(results)
    ui.console.print(
        ui.summary_panel(
            passed,
            n_trials,
            mean_reward,
            wall,
            input_tokens=usage.input_tokens if usage.calls else None,
            output_tokens=usage.output_tokens if usage.calls else None,
            cost_usd=usage.cost_usd,
        )
    )
    if saved:
        if link:
            ui.hint(f"↳ view in dashboard: {saved}")
        else:
            ui.hint(f"↳ artifacts: {saved}")
            ui.hint("↳ browse local Vault: tracetensor vault list")
    ui.console.print()


def _run_remote(
    server: str,
    token: str | None,
    task_dir: Path,
    agent: str,
    model: str | None,
    n_trials: int,
    conc: int | None,
    json_out: bool,
    open_dashboard: bool = False,
) -> tuple[list, float, str]:
    """Upload the task to a `tracetensor serve` instance, enqueue a run, and poll
    to completion. The run lives in the server's DB → visible in the dashboard."""
    from app.cli import client

    base = server.rstrip("/")
    t0 = time.time()
    if not json_out:
        ui.hint("Requires `tracetensor serve` in another terminal (same machine).")
    try:
        client.check_health(base, token)
        if json_out:
            task_id = client.upload_task(base, task_dir, token)
        else:
            with ui.console.status("[muted]uploading task to server…", spinner="dots"):
                task_id = client.upload_task(base, task_dir, token)
        job_id = client.start_examination(
            base,
            task_id,
            {"agent": agent, "model": model, "n_trials": n_trials, "concurrency": conc},
            token,
        )
    except client.ServerError as e:
        ui.error(str(e))
        if "cannot reach" in str(e).lower():
            ui.hint("Start the server first:  tracetensor serve")
            ui.hint("Then re-run with --server http://localhost:8000")
        raise typer.Exit(1) from e

    job_url = f"{base}/#job/{job_id}"
    if not json_out:
        ui.hint(f"↳ watch live: {job_url}")
        if open_dashboard:
            webbrowser.open(job_url)

    job: dict = {}

    def _poll_once() -> dict:
        return client.get_job(base, job_id, token)

    if json_out:
        while True:
            job = _poll_once()
            if job.get("status") in JobStatus.terminal():
                break
            time.sleep(1.0)
    else:
        with Progress(
            SpinnerColumn(style="accent"),
            TextColumn("[muted]running on server"),
            BarColumn(complete_style="accent", finished_style="ok"),
            TextColumn("[muted]{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=ui.console,
            transient=True,
        ) as prog:
            task = prog.add_task("run", total=n_trials)
            while True:
                job = _poll_once()
                prog.update(task, completed=job.get("trials_completed", 0))
                if job.get("status") in JobStatus.terminal():
                    prog.update(task, completed=n_trials)
                    break
                time.sleep(0.8)

    results = [
        {
            "n": t.get("trial_num", i),
            "status": t.get("status"),
            "passed": t.get("passed"),
            "reward": t.get("reward"),
            "duration_s": t.get("duration_s"),
            "steps": len((t.get("trajectory") or {}).get("steps", []) or []),
        }
        for i, t in enumerate(job.get("trials", []))
    ]
    return results, time.time() - t0, job_url


def _run_all(fn: Callable[[int], dict], n: int, conc: int) -> Iterator[dict]:
    """Yield each trial result as it completes (bounded parallelism)."""
    if conc <= 1:
        for i in range(n):
            yield fn(i)
        return
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futs = [pool.submit(fn, i) for i in range(n)]
        for fut in as_completed(futs):
            yield fut.result()


def _save(
    out_dir: Path,
    name: str,
    agent: str,
    model: str | None,
    results: list,
    passed: int,
    mean_reward: float | None,
    wall: float,
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40]
    run_dir = Path(out_dir) / f"{safe}-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "task": name,
                "agent": agent,
                "model": model,
                "n_trials": len(results),
                "passed": passed,
                "mean_reward": mean_reward,
                "wall_s": round(wall, 2),
                "trials": results,
            },
            indent=2,
            default=str,
        )
    )
    return run_dir
