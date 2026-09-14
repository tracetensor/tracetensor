"""
`tracetensor dataset` — run or fetch a whole dataset (a directory of tasks).

    tracetensor dataset run  <dir> -a mini-swe -m openai/gpt-4.1   # run every task, aggregate
    tracetensor dataset pull <source> -o tasks/<name>             # fetch a dataset locally

`run` executes every task under <dir> through the same core as `run` (run_trial)
and prints an aggregate scoreboard. `pull` resolves a dataset source —
a local path, a git repo, or `swebench[:ids|:N]` (the SWE-Bench Verified
importer) — into local task directories. This is the generalized, CLI form of
the SWE-Bench batch runner + importer.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

import typer

from app.cli import console as ui

dataset_app = typer.Typer(
    help="Run or fetch a dataset (a directory of tasks).", no_args_is_help=True
)


def _task_dirs(root: Path) -> list[Path]:
    """Task dirs under `root` (each has a task.toml). `root` itself counts if it is one."""
    if (root / "task.toml").exists():
        return [root]
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "task.toml").exists())


@dataset_app.command("run")
def run_dataset(
    path: Path = typer.Argument(..., help="Directory of tasks (or a single task)."),
    agent: str = typer.Option("mini-swe", "-a", "--agent", help="Agent to run on every task."),
    model: Optional[str] = typer.Option(None, "-m", "--model", help="Model id."),
    backend: str = typer.Option("docker", "--backend", help="Execution backend."),
    timeout: float = typer.Option(1800.0, "--timeout", help="Per-task agent/verifier timeout (s) — used when task.toml has no [agent].timeout_sec."),
    timeout_scale: float = typer.Option(
        1.0,
        "--timeout-scale",
        min=0.1,
        max=100.0,
        help="Multiply per-task timeouts from task.toml (e.g. 2.0 for harder tasks).",
    ),
    fail_fast: Optional[int] = typer.Option(
        None, "--fail-fast", help="Stop after N consecutive task failures."
    ),
    baseline: Optional[Path] = typer.Option(
        None, "--baseline", help="Path to a previous result.json — fail if any task reward drops by more than --fail-if-drops."
    ),
    fail_if_drops: float = typer.Option(
        0.05, "--fail-if-drops", help="Reward drop threshold that triggers CI failure (default 5%)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Run one agent across every task in a dataset and grade each trial."""
    from app.cli.backends import ensure_backend_ready
    from app.services.task_parser import parse_task_toml
    from app.services.trial_hints import enrich_trial_result, failure_hint_from_result
    from app.services.trial_runner import prebuild, run_trial

    resolved_backend = ensure_backend_ready(backend)

    if not path.exists():
        ui.error(f"No such path: {path}")
        raise typer.Exit(2)
    dirs = _task_dirs(path)
    if not dirs:
        ui.error(f"No tasks (dirs with task.toml) under {path}")
        raise typer.Exit(2)

    if not json_out:
        ui.console.print()
        ui.console.print(
            ui.kv_panel(
                "dataset run",
                [
                    ("tasks", str(len(dirs))),
                    ("agent", agent + (f" · {model}" if model else "")),
                    ("backend", resolved_backend),
                ],
            )
        )

    results: list[dict] = []
    consecutive_failures = 0

    def _run_one(d: Path) -> None:
        ipath = d / "instruction.md"
        instruction = ipath.read_text(errors="replace") if ipath.exists() else ""
        task_agent_to = timeout
        task_verifier_to = timeout
        tpath = d / "task.toml"
        if tpath.exists():
            try:
                tcfg = parse_task_toml(tpath.read_bytes(), d)
                task_agent_to = (tcfg.agent.timeout_sec or timeout) * timeout_scale
                task_verifier_to = (tcfg.verifier.timeout_sec or timeout) * timeout_scale
            except Exception:
                pass
        elif timeout_scale != 1.0:
            task_agent_to = timeout * timeout_scale
            task_verifier_to = timeout * timeout_scale
        t0 = time.time()
        try:
            prebuild(d, resolved_backend)
            out = run_trial(
                task_dir=d,
                instruction=instruction,
                agent_name=agent,
                model=model,
                backend=resolved_backend,
                agent_timeout=task_agent_to,
                verifier_timeout=task_verifier_to,
            )
        except Exception as e:
            return {"task": d.name, "error": str(e)[:200]}
        usage = (out.trajectory or {}).get("llm_usage_summary") or {}
        return enrich_trial_result(
            {
                "task": d.name,
                "reward": out.reward,
                "passed": bool(out.passed),
                "wall_s": round(time.time() - t0, 1),
                "error": out.error,
                "trajectory": out.trajectory,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }
        )

    if json_out:
        for d in dirs:
            r = _run_one(d)
            results.append(r)
    else:
        from rich.live import Live
        from rich.table import Table as RichTable
        from rich.text import Text
        from rich.console import Group

        live_table = RichTable(box=None, pad_edge=False, expand=False)
        live_table.add_column("", width=6)
        live_table.add_column("Task")
        live_table.add_column("Reward", justify="right")
        live_table.add_column("Time", justify="right", style="muted")
        live_table.add_column("Tokens", justify="right", style="muted")
        footer = Text()

        with Live(Group(live_table, footer), console=ui.console, refresh_per_second=4):
            for d in dirs:
                r = _run_one(d)
                results.append(r)

                if "reward" not in r:
                    live_table.add_row("[bad]ERR[/]", d.name[:36], "—", "—", "—")
                    consecutive_failures += 1
                else:
                    passed_task = r.get("passed")
                    mark = "[ok]PASS[/]" if passed_task else "[bad]FAIL[/]"
                    reward_s = f"{r['reward']:.3f}"
                    wall_s = f"{r.get('wall_s', 0):.1f}s"
                    in_t = r.get("input_tokens")
                    out_t = r.get("output_tokens")
                    tok = f"{in_t or 0}/{out_t or 0}" if in_t is not None or out_t is not None else "—"
                    hint = failure_hint_from_result(r) or ""
                    task_label = d.name[:36] + (f"  [muted]— {hint[:40]}[/]" if hint and not passed_task else "")
                    live_table.add_row(mark, task_label, reward_s, wall_s, tok)
                    consecutive_failures = consecutive_failures + 1 if not passed_task else 0

                ran_so_far = [x for x in results if "reward" in x]
                p_so_far = sum(1 for x in ran_so_far if x.get("passed"))
                mean_r = (sum(x["reward"] for x in ran_so_far) / len(ran_so_far)) if ran_so_far else 0.0
                footer = Text(
                    f"  {p_so_far}/{len(ran_so_far)} done · mean reward {mean_r:.3f}",
                    style="muted",
                )

                if fail_fast is not None and consecutive_failures >= fail_fast:
                    footer = Text(
                        f"  ⚠ stopped after {fail_fast} consecutive failures",
                        style="bad",
                    )
                    break

    ran = [r for r in results if "reward" in r]
    passed = sum(1 for r in ran if r.get("passed"))

    if json_out:
        print(json.dumps({"tasks": len(dirs), "results": results}, indent=2, default=str))
    else:
        ui.console.print()
        ui.console.print(ui.summary_panel(passed, len(ran), None, 0.0))
        ui.console.print()

    # ── Baseline regression check ──────────────────────────────────────────────
    exit_code = 0 if ran and passed == len(ran) else 1
    if baseline is not None:
        try:
            old_results = json.loads(baseline.read_text())
            old_by_task = {r["task"]: r["reward"] for r in old_results.get("results", []) if "reward" in r}
        except Exception as e:
            ui.error(f"Could not read baseline {baseline}: {e}")
            raise typer.Exit(2) from e

        regressions = []
        improvements = []
        for r in ran:
            old_r = old_by_task.get(r["task"])
            if old_r is None:
                continue
            drop = old_r - r["reward"]
            if drop > fail_if_drops:
                regressions.append((r["task"], old_r, r["reward"], drop))
            elif r["reward"] > old_r + 0.01:
                improvements.append((r["task"], old_r, r["reward"]))

        if not json_out:
            if improvements or regressions:
                ui.console.print()
                t = Table(box=None, pad_edge=False, expand=False)
                t.add_column("Task")
                t.add_column("Baseline", justify="right")
                t.add_column("Now", justify="right")
                t.add_column("Δ", justify="right")
                for task, old, new, drop in regressions:
                    t.add_row(task[:36], f"{old:.3f}", f"[bad]{new:.3f}[/]", f"[bad]−{drop:.3f}[/]")
                for task, old, new in improvements:
                    t.add_row(task[:36], f"{old:.3f}", f"[ok]{new:.3f}[/]", f"[ok]+{new - old:.3f}[/]")
                ui.console.print(t)

        if regressions:
            if not json_out:
                ui.console.print(f"\n[bad]✗ {len(regressions)} regression(s) exceed --fail-if-drops={fail_if_drops}[/]\n")
            exit_code = 1

    raise typer.Exit(exit_code)


@dataset_app.command("pull")
def pull_dataset(
    source: str = typer.Argument(
        ..., help="Source: a local path, a git URL, or 'swebench[:id1,id2 | :N]'."
    ),
    out: Path = typer.Option(Path("tasks"), "-o", "--out", help="Where to write task dirs."),
) -> None:
    """Fetch a dataset into local task directories (local path, git URL, or swebench)."""
    import shutil

    out.mkdir(parents=True, exist_ok=True)

    # 1) SWE-Bench Verified via the importer (needs the .venv-swebench toolenv).
    if source == "swebench" or source.startswith("swebench:"):
        arg = source.split(":", 1)[1] if ":" in source else ""
        project = Path(__file__).resolve().parents[3]
        venv_py = project / ".venv-swebench" / "bin" / "python"
        importer = project / "scripts" / "import_swebench.py"
        if not venv_py.exists() or not importer.exists():
            ui.error(
                "SWE-Bench toolenv missing. Create it: python3.11 -m venv .venv-swebench && "
                ".venv-swebench/bin/pip install swebench"
            )
            raise typer.Exit(2)
        cmd = [str(venv_py), str(importer), "--out", str(out / "swebench")]
        if arg.isdigit():
            cmd += ["--n", arg]
        elif arg:
            cmd += ["--instances", arg]
        ui.hint("importing SWE-Bench (this pulls from HuggingFace)…")
        raise typer.Exit(subprocess.run(cmd).returncode)

    # 2) git repo of tasks.
    if source.startswith(("http://", "https://", "git@")) or source.endswith(".git"):
        dest = out / (source.rstrip("/").split("/")[-1].removesuffix(".git") or "dataset")
        ui.hint(f"cloning {source} → {dest}")
        rc = subprocess.run(["git", "clone", "--depth", "1", source, str(dest)]).returncode
        raise typer.Exit(rc)

    # 3) local path — copy its task dirs in.
    src = Path(source.split(":", 1)[1] if source.startswith("local:") else source)
    if not src.exists():
        ui.error(f"Unknown source (not swebench, not a git URL, not a local path): {source}")
        raise typer.Exit(2)
    dirs = _task_dirs(src)
    if not dirs:
        ui.error(f"No tasks under {src}")
        raise typer.Exit(2)
    dest_root = out / src.name
    for d in dirs:
        shutil.copytree(d, dest_root / d.name, dirs_exist_ok=True)
    ui.console.print(f"[ok]✓[/] pulled {len(dirs)} task(s) → {dest_root}")
    raise typer.Exit(0)


@dataset_app.command("compare")
def compare_dataset(
    path: Path = typer.Argument(..., help="Directory of tasks (or a single task)."),
    agents: list[str] = typer.Option(..., "-a", "--agent", help="Agents to compare (repeat flag)."),
    model: Optional[str] = typer.Option(None, "-m", "--model", help="Model id (shared across agents)."),
    backend: str = typer.Option("docker", "--backend", help="Execution backend."),
    timeout: float = typer.Option(1800.0, "--timeout", help="Per-task timeout (s)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Run multiple agents on the same dataset and compare results side-by-side."""
    import concurrent.futures

    from app.cli.backends import ensure_backend_ready
    from app.services.trial_runner import prebuild, run_trial

    resolved_backend = ensure_backend_ready(backend)

    if not path.exists():
        ui.error(f"No such path: {path}")
        raise typer.Exit(2)
    dirs = _task_dirs(path)
    if not dirs:
        ui.error(f"No tasks (dirs with task.toml) under {path}")
        raise typer.Exit(2)
    if not agents:
        ui.error("Specify at least one agent with -a")
        raise typer.Exit(2)

    if not json_out:
        ui.console.print()
        ui.console.print(
            ui.kv_panel(
                "dataset compare",
                [
                    ("tasks", str(len(dirs))),
                    ("agents", " · ".join(agents)),
                    ("backend", resolved_backend),
                ],
            )
        )

    # Build work list: all (task_dir, agent) pairs.
    pairs = [(d, a) for d in dirs for a in agents]

    def _run_pair(pair):
        d, ag = pair
        ipath = d / "instruction.md"
        instruction = ipath.read_text(errors="replace") if ipath.exists() else ""
        try:
            prebuild(d, resolved_backend)
            out = run_trial(
                task_dir=d,
                instruction=instruction,
                agent_name=ag,
                model=model,
                backend=resolved_backend,
                agent_timeout=timeout,
                verifier_timeout=timeout,
            )
            return (d.name, ag, out.reward, bool(out.passed), None)
        except Exception as e:
            return (d.name, ag, None, False, str(e)[:120])

    results: dict[str, dict[str, dict]] = {}
    max_workers = min(8, len(pairs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for task, ag, reward, passed, err in pool.map(_run_pair, pairs):
            results.setdefault(task, {})[ag] = {"reward": reward, "passed": passed, "error": err}

    if json_out:
        print(json.dumps({"tasks": [d.name for d in dirs], "agents": agents, "results": results}, indent=2, default=str))
        raise typer.Exit(0)

    # ── Render comparison table ────────────────────────────────────────────────
    t = Table(box=None, pad_edge=False, expand=False)
    t.add_column("Task")
    for ag in agents:
        t.add_column(ag, justify="right")
    t.add_column("Winner", justify="left", style="muted")

    agent_totals: dict[str, list[float]] = {a: [] for a in agents}
    for d in dirs:
        task = d.name
        row_cells = [task[:32]]
        row_rewards = {}
        for ag in agents:
            r = results.get(task, {}).get(ag, {})
            reward = r.get("reward")
            if reward is None:
                row_cells.append("[muted]ERR[/]")
            elif r.get("passed"):
                row_cells.append(f"[ok]{reward:.3f} ✓[/]")
                agent_totals[ag].append(reward)
                row_rewards[ag] = reward
            else:
                row_cells.append(f"[bad]{reward:.3f} ✗[/]")
                agent_totals[ag].append(reward)
                row_rewards[ag] = reward

        if row_rewards:
            best_ag = max(row_rewards, key=row_rewards.__getitem__)
            winner = f"[accent]{best_ag} ★[/]" if len(row_rewards) > 1 and len(set(row_rewards.values())) > 1 else "—"
        else:
            winner = "—"
        row_cells.append(winner)
        t.add_row(*row_cells)

    # Footer: mean per agent
    mean_cells = ["[muted]mean[/]"]
    mean_rewards = {}
    for ag in agents:
        vals = agent_totals[ag]
        m = sum(vals) / len(vals) if vals else 0.0
        mean_rewards[ag] = m
        mean_cells.append(f"[muted]{m:.3f}[/]")
    best_overall = max(mean_rewards, key=mean_rewards.__getitem__) if mean_rewards else None
    mean_cells.append(f"[ok]{best_overall}[/]" if best_overall else "")
    t.add_row(*mean_cells)

    ui.console.print()
    ui.console.print(t)
    ui.console.print()
    raise typer.Exit(0)
