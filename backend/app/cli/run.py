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
    """Point at a task directory.

    Accepts both native format (task.toml / instruction.md) and
    HUD-compatible format (env.py).
    """
    if not path.exists():
        ui.error(f"No such path: {path}")
        raise typer.Exit(2)
    has_native = (path / "task.toml").exists() or (path / "instruction.md").exists()
    has_hud = (path / "env.py").exists()
    if not has_native and not has_hud:
        ui.error(
            f"{path} doesn't look like a task (no task.toml / instruction.md / env.py). "
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
    backend: str = typer.Option(
        "docker",
        "--backend",
        help="Execution backend: docker, podman, daytona, modal, e2b, runloop, novita.",
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
    timeout_scale: float = typer.Option(
        1.0,
        "--timeout-scale",
        min=0.1,
        max=100.0,
        help="Multiply agent + verifier timeouts (e.g. 2.0 for harder tasks, 0.5 for quick smoke tests).",
    ),
    agent_timeout: Optional[float] = typer.Option(
        None,
        "--agent-timeout",
        min=1.0,
        help="Override agent timeout in seconds (takes precedence over task.toml + --timeout-scale).",
    ),
    verifier_timeout: Optional[float] = typer.Option(
        None,
        "--verifier-timeout",
        min=1.0,
        help="Override verifier timeout in seconds (takes precedence over task.toml + --timeout-scale).",
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
    from app.cli.backends import ensure_backend_ready
    from app.core.config import settings
    from app.services.agents import AgentConfigError, resolve_agent
    from app.services.run_warnings import run_warnings
    from app.services.task_parser import TaskParseError, parse_task_toml
    from app.services.trial_hints import enrich_trial_result
    from app.services.trial_runner import prebuild, run_trial

    task_dir = _resolve_task(path)

    # ── HUD-compatible format (env.py) ────────────────────────────────────────
    from app.services.format_detector import detect_format, UnknownTaskFormat
    try:
        task_format = detect_format(task_dir)
    except UnknownTaskFormat as e:
        ui.error(str(e))
        raise typer.Exit(2) from e

    if task_format == "hud":
        if server:
            # Upload the env.py directory to the server and run it through the
            # dashboard job queue — same flow as native format.
            remote, wall, job_url = _run_remote(
                server,
                token,
                task_dir,
                agent,
                model,
                n_trials,
                1,
                backend,
                json_out,
                open_dashboard=open_dashboard,
            )
            _report(
                task_dir.name, agent, model, n_trials,
                remote, wall, json_out, saved=job_url, link=True,
            )
        else:
            _run_hud(
                task_dir=task_dir,
                agent=agent,
                model=model,
                n_trials=n_trials,
                timeout=agent_timeout or (120.0 * timeout_scale),
                output=output,
                no_save=no_save,
                json_out=json_out,
            )
        raise typer.Exit(0)

    # ── Native TraceTensor format (task.toml) ─────────────────────────────────
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
            agent_to = (parsed_cfg.agent.timeout_sec or 120.0) * timeout_scale
            verifier_to = (parsed_cfg.verifier.timeout_sec or 120.0) * timeout_scale
        except TaskParseError as e:
            ui.error(f"Bad task.toml: {e}")
            raise typer.Exit(2) from e
    elif timeout_scale != 1.0:
        agent_to *= timeout_scale
        verifier_to *= timeout_scale

    # Direct timeout overrides take precedence over task.toml values and --timeout-scale.
    if agent_timeout is not None:
        agent_to = agent_timeout
    if verifier_timeout is not None:
        verifier_to = verifier_timeout

    try:
        resolve_agent(agent, model, settings)
    except AgentConfigError as e:
        ui.error(str(e))
        raise typer.Exit(2) from e

    conc = concurrency or min(4, n_trials)
    resolved_backend = ensure_backend_ready(backend)

    # Refuse an unrunnable task before anything is provisioned. Left to the trial
    # this surfaces only after a sandbox exists — and on a cloud backend, after
    # it has been paid for.
    if parsed_cfg:
        from app.services.backend_capabilities import unsupported_features

        blockers = unsupported_features(resolved_backend, parsed_cfg)
        if blockers:
            from rich.markup import escape

            ui.console.print()
            ui.console.print(f"[bad]✗[/] this task cannot run on the {resolved_backend} backend:")
            for reason in blockers:
                # Escaped: these messages name TOML tables like [agent] and
                # [environment], which Rich would otherwise read as style tags
                # and silently delete — removing the very words that say which
                # setting to change.
                ui.console.print(f"  [bad]•[/] {escape(reason)}")
            raise typer.Exit(1)

    if n_trials > 10 and not json_out:
        ui.warn(f"{n_trials} trials will run — confirm cost/time before large batches.")

    if not json_out:
        rows = [
            ("task", name),
            ("agent", agent + (f" · {model}" if model else "")),
            ("trials", f"{n_trials}  (×{conc} parallel)"),
            ("timeouts", f"agent {int(agent_to)}s · verifier {int(verifier_to)}s"
                     + (" (--agent-timeout)" if agent_timeout is not None else "")
                     + (" (--verifier-timeout)" if verifier_timeout is not None else "")),
            ("where", server if server else f"local · {resolved_backend}"),
        ]
        ui.console.print()
        ui.console.print(ui.kv_panel("run", rows))
        if parsed_cfg and resolved_backend in ("docker", "podman"):
            from app.services.local_preflight import assess_local_run

            seen: set[str] = set()
            for w in run_warnings(task_dir, parsed_cfg, agent):
                ui.warn(w)
                seen.add(w)
            tier, preflight = assess_local_run(task_dir, parsed_cfg)
            for w in preflight:
                if w not in seen:
                    ui.warn(w)
            if tier.value != "native":
                ui.hint(f"Local run tier: {tier.value}")
        elif parsed_cfg:
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
            resolved_backend,
            json_out,
            open_dashboard=open_dashboard,
        )
        _report(name, agent, model, n_trials, remote, wall, json_out, saved=job_url, link=True)
        raise typer.Exit(0 if any(r.get("passed") for r in remote) else 1)

    # ---- local: build the image once, run trials here (no server needed) -----
    t0 = time.time()
    try:
        if not json_out:
            with ui.console.status("[muted]preparing sandbox…", spinner="dots"):
                prebuild(task_dir, resolved_backend)
        else:
            prebuild(task_dir, resolved_backend)
    except Exception as e:
        ui.error(f"Could not prepare the sandbox: {e}")
        if resolved_backend in ("docker", "podman"):
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
            backend=resolved_backend,
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
        saved_to = str(_save(output, name, agent, model, resolved_backend, results, passed, mean_reward, wall))

    _report(name, agent, model, n_trials, results, wall, json_out, saved=saved_to, link=False)
    # Nonzero exit if nothing passed, so CI can gate on it.
    raise typer.Exit(0 if passed > 0 else 1)


def _run_hud(
    task_dir: Path,
    agent: str,
    model: str | None,
    n_trials: int,
    timeout: float,
    output: Path,
    no_save: bool,
    json_out: bool,
) -> None:
    """Execute all tasks in a HUD-format env.py directory."""
    import time
    from app.services.hud_adapter import load_hud_env, HudLoadError
    from app.services.hud_runner import run_hud_task_sync
    from app.services.trial_hints import enrich_trial_result

    # Parse provider / model from the agent string (same convention as native).
    # HUD text tasks go through a single-turn LLM call; the agent string is
    # interpreted as a provider name ("openai", "anthropic", "openrouter").
    provider = agent
    try:
        from app.services import llm
        provider = llm.canonical_provider(agent)
    except Exception:
        pass

    try:
        loaded = load_hud_env(task_dir)
    except HudLoadError as e:
        ui.error(f"Could not load env.py: {e}")
        raise typer.Exit(2) from e

    env_name = loaded.env.name
    tasks = loaded.tasks
    if not tasks:
        ui.warn("No tasks found in tasks.py — nothing to run.")
        return

    if not json_out:
        ui.console.print()
        ui.console.print(ui.kv_panel("run [hud]", [
            ("env", env_name),
            ("tasks", str(len(tasks))),
            ("agent", f"{provider}" + (f" · {model}" if model else "")),
            ("trials", f"{n_trials} per task"),
            ("timeout", f"{int(timeout)}s"),
        ]))

    all_results: list[dict] = []
    wall_start = time.time()

    for task_idx, task in enumerate(tasks):
        task_label = f"{env_name}/{task.template_id}({', '.join(f'{k}={v!r}' for k, v in list(task.kwargs.items())[:2])})"
        for trial_n in range(n_trials):
            if not json_out:
                ui.console.print(f"  [muted]running[/] {task_label} trial #{trial_n + 1}…")

            outcome = run_hud_task_sync(
                task,
                provider=provider,
                model=model,
                timeout=timeout,
                task_dir=task_dir,
            )
            result = enrich_trial_result({
                "n": task_idx * n_trials + trial_n,
                "task": task_label,
                "status": outcome.status,
                "passed": outcome.passed,
                "reward": outcome.reward,
                "duration_s": outcome.duration_s,
                "steps": 1,
                "error": outcome.error,
                "trajectory": outcome.trajectory,
                "reward_payload": outcome.reward_payload,
            })
            all_results.append(result)
            if not json_out:
                icon = "[ok]✓[/]" if outcome.passed else "[bad]✗[/]"
                answer = (outcome.trajectory or {}).get("answer", "")[:60]
                ui.console.print(f"    {icon} reward={outcome.reward:.2f}  answer={answer!r}")

    wall = time.time() - wall_start
    passed = sum(1 for r in all_results if r.get("passed"))
    rewards = [r["reward"] for r in all_results if r.get("reward") is not None]
    mean_reward = sum(rewards) / len(rewards) if rewards else None

    saved_to = None
    if not no_save:
        saved_to = str(_save(output, env_name, provider, model, "hud", all_results, passed, mean_reward, wall))

    _report(env_name, provider, model, len(all_results), all_results, wall, json_out,
            saved=saved_to, link=False)


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
    backend: str,
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
            {
                "agent": agent,
                "model": model,
                "n_trials": n_trials,
                "concurrency": conc,
                "backend": backend,
            },
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
    backend: str,
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
                "backend": backend,
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
