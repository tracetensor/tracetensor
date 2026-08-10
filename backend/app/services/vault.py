"""
Vault — local-first layer over saved evaluation runs.

Phase 1: browse / show / export jobs and CLI `runs/*/result.json` artifacts.
Usage (tokens / latency) is rolled up from trial `trajectory.llm_usage_summary`
already written by trial_runner — no separate billing store.

Remote share (`vault push`) is Phase 2.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.services.cost import cost_enabled, normalize_cost


@dataclass
class UsageRollup:
    """Aggregated LLM usage across one or more trials."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: Optional[float] = None
    duration_s: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def usage_from_trajectory(traj: Optional[dict]) -> UsageRollup:
    """Read one trial's llm_usage_summary (missing → zeros)."""
    if not traj or not isinstance(traj, dict):
        return UsageRollup()
    u = traj.get("llm_usage_summary") or {}
    if not isinstance(u, dict):
        return UsageRollup()
    return UsageRollup(
        calls=int(u.get("calls") or 0),
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        latency_ms=float(u.get("latency_ms") or 0.0),
        cost_usd=normalize_cost(u.get("cost_usd")),
    )


def rollup_usage(
    trials: Sequence[Any],
    *,
    trajectory_attr: str = "trajectory",
    duration_attr: str = "duration_s",
) -> UsageRollup:
    """Sum usage across ORM Trial objects or dict-shaped trials (CLI result.json)."""
    summaries: List[UsageRollup] = []
    durations: List[float] = []
    for t in trials:
        if isinstance(t, dict):
            traj = t.get("trajectory")
            dur = t.get("duration_s")
        else:
            traj = getattr(t, trajectory_attr, None)
            dur = getattr(t, duration_attr, None)
        summaries.append(usage_from_trajectory(traj if isinstance(traj, dict) else None))
        if dur is not None:
            try:
                durations.append(float(dur))
            except (TypeError, ValueError):
                pass

    if not summaries:
        return UsageRollup(duration_s=sum(durations) if durations else None)

    costs = [s.cost_usd for s in summaries]
    # Match trial_runner: unknown cost anywhere → None (not a fake 0).
    cost_known = all(c is not None for c in costs) and any(s.calls for s in summaries)
    return UsageRollup(
        calls=sum(s.calls for s in summaries),
        input_tokens=sum(s.input_tokens for s in summaries),
        output_tokens=sum(s.output_tokens for s in summaries),
        latency_ms=round(sum(s.latency_ms for s in summaries), 1),
        cost_usd=normalize_cost(round(sum(c or 0.0 for c in costs), 6) if cost_known else None),
        duration_s=round(sum(durations), 3) if durations else None,
    )


def summary_usage_fields(usage: UsageRollup) -> Dict[str, Any]:
    """Fields to attach on JobSummary / Vault list rows."""
    return {
        "duration_s": usage.duration_s,
        "input_tokens": usage.input_tokens if usage.calls else None,
        "output_tokens": usage.output_tokens if usage.calls else None,
        "cost_usd": usage.cost_usd,
    }


# ---------------------------------------------------------------------------
# Local CLI runs/ artifacts
# ---------------------------------------------------------------------------


def _read_result_json(run_dir: Path) -> Optional[dict]:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_local_runs(runs_dir: Path) -> List[dict]:
    """Scan `runs/*/result.json` into Vault list rows (newest first)."""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    rows: List[dict] = []
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        data = _read_result_json(child)
        if not data:
            continue
        trials = data.get("trials") or []
        usage = rollup_usage(trials if isinstance(trials, list) else [])
        n_trials = int(data.get("n_trials") or len(trials) or 0)
        passed = int(data.get("passed") or 0)
        wall = data.get("wall_s")
        rows.append(
            {
                "id": child.name,
                "path": str(child.resolve()),
                "source": "local",
                "task": data.get("task"),
                "agent": data.get("agent"),
                "model": data.get("model"),
                "n_trials": n_trials,
                "passed": passed,
                "pass_rate": (passed / n_trials) if n_trials else None,
                "wall_s": wall,
                **summary_usage_fields(usage),
                # Prefer wall clock from result when present.
                "duration_s": wall if wall is not None else usage.duration_s,
            }
        )
    return rows


def resolve_local_run(runs_dir: Path, run_id: str) -> Path:
    """Resolve a Vault id to a run directory (name, relative path, or absolute)."""
    candidate = Path(run_id)
    if candidate.is_dir() and (candidate / "result.json").is_file():
        return candidate.resolve()
    under = Path(runs_dir) / run_id
    if under.is_dir() and (under / "result.json").is_file():
        return under.resolve()
    raise FileNotFoundError(f"No local Vault run matching {run_id!r} under {runs_dir}")


def load_local_run(run_dir: Path) -> dict:
    data = _read_result_json(run_dir)
    if not data:
        raise FileNotFoundError(f"Missing or invalid result.json in {run_dir}")
    trials = data.get("trials") or []
    usage = rollup_usage(trials if isinstance(trials, list) else [])
    return {
        "id": run_dir.name,
        "path": str(run_dir.resolve()),
        "source": "local",
        **data,
        "usage": usage.as_dict(),
    }


def export_local_run(run_dir: Path, out_dir: Path) -> Path:
    """Copy a local run archive into out_dir/<run-name>/."""
    run_dir = run_dir.resolve()
    dest = Path(out_dir) / run_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    src = run_dir / "result.json"
    if not src.is_file():
        raise FileNotFoundError(f"No result.json in {run_dir}")
    shutil.copy2(src, dest / "result.json")
    # Optional sidecar files if present.
    for name in ("trajectory.json", "README.md"):
        extra = run_dir / name
        if extra.is_file():
            shutil.copy2(extra, dest / name)
    return dest


def export_job_dict(job: dict, out_dir: Path, job_id: str) -> Path:
    """Write a server job payload as a portable JSON file under out_dir."""
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"job-{job_id}.json"
    path.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    return path


def format_tokens(n: Optional[int]) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_cost(c: Optional[float]) -> str:
    if not cost_enabled():
        return ""
    if c is None:
        return "—"
    if c < 0.01:
        return f"${c:.4f}"
    return f"${c:.3f}"


def format_usage_line(
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cost_usd: Optional[float] = None,
) -> str:
    """Tokens (+ optional cost when tracking is enabled)."""
    line = f"{format_tokens(input_tokens)} in · {format_tokens(output_tokens)} out"
    cost_s = format_cost(cost_usd)
    if cost_s:
        return f"{line} · {cost_s}"
    return line
