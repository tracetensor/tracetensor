#!/usr/bin/env python3
"""Build website/assets/exhibits.json from real task files and real run artifacts.

Nothing here is authored or estimated. Task content is read verbatim from
tasks/exhibits/, run content verbatim from runs-exhibits/*/result.json. If a
scenario has no run yet it is emitted with runs: [] rather than a placeholder,
so the page can say "not run" instead of inventing one.

Usage:  python3 scripts/build_exhibits.py
"""

from __future__ import annotations

import json
import pathlib
import sys

try:
    import tomllib
except ImportError:  # py3.10
    import tomli as tomllib  # type: ignore

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def backfill_tokens(agent: str, native: dict | None) -> tuple:
    """Recover token counts from a run's stored native trajectory.

    Runs recorded before the adapters learned to map usage have input/output of
    0 even though the raw vendor payload was saved alongside them. Re-parsing
    that payload with the current adapters recovers the real numbers — it reads
    what the vendor already reported and never computes or estimates anything.
    Returns (None, None) when the adapter genuinely has nothing to report.
    """
    if not isinstance(native, dict):
        return None, None
    try:
        if agent == "claude-code":
            from app.services.agents.claude_code import _tokens_from_model_usage

            return _tokens_from_model_usage(native)
        if agent == "codex":
            from app.services.agents.codex import _tokens_from_turns

            return _tokens_from_turns(native.get("events") or [])
    except Exception:
        return None, None
    return None, None
TASKS = ROOT / "tasks" / "exhibits"
RUNS = ROOT / "runs-exhibits"
# Emitted as JS, not JSON: a <script src> works over file:// while fetch() is
# blocked there by CORS, so the page previews locally without a server.
OUT = ROOT / "website" / "assets" / "exhibits.js"

# Files we never inline: binaries and anything large enough to bloat the page.
MAX_FILE_BYTES = 60_000
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".bin", ".so"}


def read_text(p: pathlib.Path) -> str | None:
    if p.suffix.lower() in SKIP_SUFFIXES:
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        head = raw[:MAX_FILE_BYTES].decode("utf-8", "replace")
        return head + f"\n\n… truncated — {len(raw):,} bytes total, showing first {MAX_FILE_BYTES:,}"
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def task_files(task_dir: pathlib.Path) -> list[dict]:
    """Every file in the task, with its real byte size — the Files tab."""
    out = []
    for p in sorted(task_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(task_dir).as_posix()
        out.append(
            {
                "path": rel,
                "bytes": p.stat().st_size,
                "text": read_text(p),
            }
        )
    return out


def collect_runs(scenario_id: str, task_dir_name: str) -> list[dict]:
    """Every recorded run whose task matches this scenario, newest last."""
    found = []
    for rp in sorted(RUNS.glob("*/result.json")):
        try:
            r = json.loads(rp.read_text())
        except (OSError, ValueError):
            continue
        # match by the source task the run recorded
        if not _matches(r, task_dir_name):
            continue
        t = r["trials"][0]
        tr = t.get("trajectory") or {}
        usage = tr.get("llm_usage_summary") or {}
        found.append(
            {
                "run_dir": rp.parent.name,
                "agent": r.get("agent"),
                "model": r.get("model"),
                "backend": r.get("backend"),
                "passed": bool(t.get("passed")),
                "reward": t.get("reward"),
                "status": t.get("status"),
                "duration_s": t.get("duration_s"),
                "wall_s": r.get("wall_s"),
                "calls": usage.get("calls"),
                "input_tokens": usage.get("input_tokens") or None,
                "output_tokens": usage.get("output_tokens") or None,
                "cost_usd": usage.get("cost_usd"),
                "guardrail_flags": tr.get("guardrail_flags") or [],
                "warnings": tr.get("warnings") or [],
                "execution": tr.get("execution") or {},
                "reward_payload": t.get("reward_payload"),
                "command": _command_for(r),
                "steps": [
                    {
                        "phase": s.get("phase"),
                        "command": s.get("command") or "",
                        "exit_code": s.get("exit_code"),
                        "duration_s": s.get("duration_s"),
                        "stdout": s.get("stdout") or "",
                        "stderr": s.get("stderr") or "",
                    }
                    for s in (tr.get("steps") or [])
                ],
            }
        )
    return found


_TASK_NAME_BY_DIR: dict[str, str] = {}


def _matches(result: dict, task_dir_name: str) -> bool:
    want = _TASK_NAME_BY_DIR.get(task_dir_name)
    return bool(want) and result.get("task") == want


def _command_for(result: dict) -> str:
    """The exact command that produced this run — never a prettied-up version."""
    parts = ["tracetensor run", f"tasks/exhibits/{result['_dir']}", "-a", result["agent"]]
    if result.get("model"):
        parts += ["-m", result["model"]]
    parts += ["-n", "1", "--backend", result.get("backend") or "docker"]
    return " ".join(parts)


def main() -> int:
    manifest = tomllib.loads((TASKS / "scenarios.toml").read_text())
    scenarios = []

    # map task dir -> the task name recorded in run artifacts
    for entry in manifest["scenario"]:
        cfg = tomllib.loads((TASKS / entry["task_dir"] / "task.toml").read_text())
        _TASK_NAME_BY_DIR[entry["task_dir"]] = cfg["task"]["name"]

    for entry in manifest["scenario"]:
        d = TASKS / entry["task_dir"]
        cfg = tomllib.loads((d / "task.toml").read_text())
        runs = []
        for rp in sorted(RUNS.glob("*/result.json")):
            try:
                r = json.loads(rp.read_text())
            except (OSError, ValueError):
                continue
            if r.get("task") != cfg["task"]["name"]:
                continue
            r["_dir"] = entry["task_dir"]
            t = r["trials"][0]
            tr = t.get("trajectory") or {}
            usage = tr.get("llm_usage_summary") or {}
            inp = usage.get("input_tokens") or None
            out = usage.get("output_tokens") or None
            if inp is None and out is None:
                inp, out = backfill_tokens(r.get("agent"), tr.get("agent_native_trajectory"))
            runs.append(
                {
                    "run_dir": rp.parent.name,
                    "agent": r.get("agent"),
                    "model": r.get("model"),
                    "backend": r.get("backend"),
                    "passed": bool(t.get("passed")),
                    "reward": t.get("reward"),
                    "duration_s": t.get("duration_s"),
                    "calls": usage.get("calls"),
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cost_usd": usage.get("cost_usd"),
                    "guardrail_flags": tr.get("guardrail_flags") or [],
                    "execution": tr.get("execution") or {},
                    "reward_payload": t.get("reward_payload"),
                    "command": _command_for(r),
                    "steps": [
                        {
                            "phase": s.get("phase"),
                            "command": s.get("command") or "",
                            "exit_code": s.get("exit_code"),
                            "duration_s": s.get("duration_s"),
                            "stdout": s.get("stdout") or "",
                            "stderr": s.get("stderr") or "",
                        }
                        for s in (tr.get("steps") or [])
                    ],
                }
            )
        runs.sort(key=lambda x: (x["agent"] or "", x["model"] or ""))

        scenarios.append(
            {
                "id": entry["id"],
                "persona": entry["persona"],
                "situation": entry["situation"],
                "job": entry["job"],
                "shows": entry["shows"],
                # written after the runs, from their numbers; absent where a
                # scenario has a single run and nothing to compare
                "finding": entry.get("finding"),
                "source": entry["source"],
                "task_dir": entry["task_dir"],
                "task_name": cfg["task"]["name"],
                "description": cfg["task"].get("description") or "",
                "keywords": cfg["task"].get("keywords") or [],
                "category": (cfg.get("metadata") or {}).get("category"),
                "docker_image": (cfg.get("environment") or {}).get("docker_image"),
                "network_mode": (cfg.get("environment") or {}).get("network_mode", "public"),
                "agent_timeout_s": (cfg.get("agent") or {}).get("timeout_sec"),
                "verifier_timeout_s": (cfg.get("verifier") or {}).get("timeout_sec"),
                "instruction": read_text(d / "instruction.md") or "",
                "readme": read_text(d / "README.md"),
                "files": task_files(d),
                "runs": runs,
            }
        )

    payload = {
        "generated_from": "tasks/exhibits + runs-exhibits (verbatim)",
        "backend": manifest.get("backend"),
        "scenarios": scenarios,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, separators=(",", ":"))
    OUT.write_text("window.__EXHIBITS=" + body + ";\n")

    total_runs = sum(len(s["runs"]) for s in scenarios)
    print(f"wrote {OUT.relative_to(ROOT)}  {OUT.stat().st_size:,} bytes")
    print(f"  {len(scenarios)} scenarios, {total_runs} runs")
    for s in scenarios:
        rs = ", ".join(
            f"{r['agent']}{'/' + r['model'] if r['model'] else ''}"
            f"{'✓' if r['passed'] else '✗'}"
            for r in s["runs"]
        ) or "NO RUNS YET"
        print(f"  {s['id']:<22} {len(s['files']):>2} files  {rs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
