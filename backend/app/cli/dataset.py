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
    timeout: float = typer.Option(1800.0, "--timeout", help="Per-task agent/verifier timeout (s)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Run one agent across every task in a dataset and grade each trial."""
    from app.services.trial_hints import enrich_trial_result, failure_hint_from_result
    from app.services.trial_runner import prebuild, run_trial

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
                [("tasks", str(len(dirs))), ("agent", agent + (f" · {model}" if model else ""))],
            )
        )

    results: list[dict] = []
    for d in dirs:
        ipath = d / "instruction.md"
        instruction = ipath.read_text(errors="replace") if ipath.exists() else ""
        t0 = time.time()
        try:
            prebuild(d, backend)
            out = run_trial(
                task_dir=d,
                instruction=instruction,
                agent_name=agent,
                model=model,
                backend=backend,
                agent_timeout=timeout,
                verifier_timeout=timeout,
            )
        except Exception as e:  # one task never aborts the dataset
            results.append({"task": d.name, "error": str(e)[:200]})
            if not json_out:
                ui.console.print(f"  [bad]ERR[/]  {d.name} — {str(e)[:80]}")
            continue
        usage = (out.trajectory or {}).get("llm_usage_summary") or {}
        results.append(
            enrich_trial_result(
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
        )
        if not json_out:
            mark = "[ok]PASS[/]" if out.passed else "[bad]FAIL[/]"
            in_t = usage.get("input_tokens")
            out_t = usage.get("output_tokens")
            tok = f"{in_t or 0}/{out_t or 0}" if in_t is not None or out_t is not None else "—"
            hint = failure_hint_from_result(results[-1]) or ""
            suffix = f"  — {hint[:60]}" if hint and not out.passed else ""
            ui.console.print(f"  {mark}  {d.name}  reward={out.reward}  tokens={tok}{suffix}")

    ran = [r for r in results if "reward" in r]
    passed = sum(1 for r in ran if r.get("passed"))
    if json_out:
        print(json.dumps({"tasks": len(dirs), "results": results}, indent=2, default=str))
    else:
        ui.console.print()
        ui.console.print(ui.summary_panel(passed, len(ran), None, 0.0))
        failed = [r for r in ran if not r.get("passed")]
        if failed:
            ui.console.print()
            ui.console.print("[bad]Failed tasks[/]")
            for r in failed:
                hint = r.get("failure_hint") or r.get("error") or "unknown"
                ui.console.print(f"  [bad]•[/] {r.get('task', '?')} — [muted]{hint}[/]")
        ui.console.print()
    raise typer.Exit(0 if ran and passed == len(ran) else 1)


@dataset_app.command("pull")
def pull_dataset(
    source: str = typer.Argument(
        ..., help="Source: a local path, a git URL, or 'swebench[:id1,id2 | :N]'."
    ),
    out: Path = typer.Option(Path("tasks"), "-o", "--out", help="Where to write task dirs."),
) -> None:
    """Fetch a dataset into local task directories (local / git / swebench)."""
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
