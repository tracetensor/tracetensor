#!/usr/bin/env python3
"""Run 15 trials across pr-suite with max 3 OpenAI models (same as test-suite batch)."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "examples" / "pr-suite"
OUT = ROOT / "backend" / "runs" / "pr-suite-batch-report.json"

MODELS = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.6-luna"]
TOTAL_TRIALS = 15
SEED = 20260807


def task_dirs() -> list[Path]:
    return sorted(d for d in SUITE.iterdir() if d.is_dir() and (d / "task.toml").exists())


def build_schedule() -> list[tuple[str, str]]:
    tasks = [d.name for d in task_dirs()]
    rng = random.Random(SEED)
    return [(rng.choice(tasks), rng.choice(MODELS)) for _ in range(TOTAL_TRIALS)]


def group_schedule(schedule: list[tuple[str, str]]) -> list[tuple[str, str, int]]:
    counts: Counter[tuple[str, str]] = Counter(schedule)
    keys = sorted(counts.keys(), key=lambda x: (x[0], x[1]))
    return [(task, model, counts[(task, model)]) for task, model in keys]


def run_batch(task: str, model: str, n: int) -> dict:
    task_path = SUITE / task
    cmd = [
        sys.executable,
        "-m",
        "app.cli.main",
        "run",
        str(task_path),
        "-a",
        "openai",
        "-m",
        model,
        "-n",
        str(n),
        "--json",
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT / "backend", capture_output=True, text=True)
    payload: dict = {
        "task": task,
        "model": model,
        "n_trials": n,
        "exit_code": proc.returncode,
        "wall_s": round(time.time() - t0, 2),
        "stderr": proc.stderr[-2000:] if proc.stderr else "",
    }
    if proc.stdout.strip():
        try:
            payload["cli"] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload["stdout_raw"] = proc.stdout[-4000:]
    return payload


def main() -> int:
    tasks = task_dirs()
    if len(tasks) != 5:
        print(f"expected 5 tasks, found {len(tasks)}", file=sys.stderr)
        return 1
    schedule = build_schedule()
    batches = group_schedule(schedule)
    print(
        json.dumps(
            {"seed": SEED, "total_trials": TOTAL_TRIALS, "models": MODELS, "batches": batches},
            indent=2,
        )
    )
    print("\n--- running batches ---\n", flush=True)

    all_trials: list[dict] = []
    batch_results: list[dict] = []
    for i, (task, model, n) in enumerate(batches, 1):
        print(f"[{i}/{len(batches)}] {task} model={model} n={n} ...", flush=True)
        res = run_batch(task, model, n)
        batch_results.append(res)
        cli = res.get("cli") or {}
        for t in cli.get("trials") or []:
            all_trials.append(
                {
                    "task": task,
                    "model": model,
                    "trial_n": t.get("n"),
                    "passed": t.get("passed"),
                    "reward": t.get("reward"),
                    "duration_s": t.get("duration_s"),
                    "steps": t.get("steps"),
                    "error": t.get("error"),
                }
            )
        print(
            f"    done exit={res['exit_code']} passed={cli.get('passed', '?')}/{n} "
            f"wall={res['wall_s']}s",
            flush=True,
        )

    by_model = defaultdict(lambda: {"passed": 0, "total": 0})
    by_task = defaultdict(lambda: {"passed": 0, "total": 0})
    for t in all_trials:
        by_model[t["model"]]["total"] += 1
        by_task[t["task"]]["total"] += 1
        if t.get("passed"):
            by_model[t["model"]]["passed"] += 1
            by_task[t["task"]]["passed"] += 1

    report = {
        "seed": SEED,
        "suite": "pr-suite",
        "total_trials": len(all_trials),
        "models": MODELS,
        "schedule": schedule,
        "batches": batches,
        "batch_results": batch_results,
        "trials": all_trials,
        "summary": {
            "by_model": dict(by_model),
            "by_task": dict(by_task),
            "overall_passed": sum(1 for t in all_trials if t.get("passed")),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT}", flush=True)
    print(json.dumps(report["summary"], indent=2), flush=True)
    return 0 if len(all_trials) == TOTAL_TRIALS else 1


if __name__ == "__main__":
    raise SystemExit(main())
