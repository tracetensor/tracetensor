"""
Vault unit + CLI fixture tests (offline — no Docker, no API keys).

Run:  cd backend && python tests/test_vault.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

from typer.testing import CliRunner  # noqa: E402

from app.cli.main import app  # noqa: E402
from app.services import vault as vault_svc  # noqa: E402
from app.services.agents import agent_status_catalog  # noqa: E402

runner = CliRunner()
passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


print("== vault rollup ==")
empty = vault_svc.usage_from_trajectory(None)
check("empty trajectory → 0 calls", empty.calls == 0 and empty.cost_usd is None)

u = vault_svc.usage_from_trajectory(
    {
        "llm_usage_summary": {
            "calls": 2,
            "input_tokens": 100,
            "output_tokens": 50,
            "latency_ms": 12.5,
            "cost_usd": 0.001,
        }
    }
)
check("usage_from_trajectory reads summary", u.calls == 2 and u.input_tokens == 100)

rollup = vault_svc.rollup_usage(
    [
        {
            "duration_s": 1.5,
            "trajectory": {
                "llm_usage_summary": {
                    "calls": 1,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.01,
                }
            },
        },
        {
            "duration_s": 2.5,
            "trajectory": {
                "llm_usage_summary": {
                    "calls": 1,
                    "input_tokens": 20,
                    "output_tokens": 15,
                    "cost_usd": 0.02,
                }
            },
        },
    ]
)
check("rollup sums tokens", rollup.input_tokens == 30 and rollup.output_tokens == 20)
# Vendor-reported costs sum. (This used to assert None, back when a global flag
# zeroed every cost; now that provider-reported dollars flow through, a rollup of
# known costs is their sum. cost.py explains why we no longer estimate.)
check("rollup sums vendor-reported cost", rollup.cost_usd == 0.03)
check("rollup sums duration", rollup.duration_s == 4.0)

unknown = vault_svc.rollup_usage(
    [
        {"trajectory": {"llm_usage_summary": {"calls": 1, "input_tokens": 1, "cost_usd": 0.1}}},
        {"trajectory": {"llm_usage_summary": {"calls": 1, "input_tokens": 1, "cost_usd": None}}},
    ]
)
check("partial cost → None (not fake 0)", unknown.cost_usd is None)

print("\n== local runs/ list / show / export ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    run_dir = root / "demo-task-20260101-120000"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "task": "demo-task",
                "agent": "openai",
                "model": "gpt-4.1-mini",
                "n_trials": 1,
                "passed": 1,
                "mean_reward": 1.0,
                "wall_s": 3.2,
                "trials": [
                    {
                        "n": 0,
                        "passed": True,
                        "reward": 1.0,
                        "duration_s": 3.0,
                        "steps": 2,
                        "trajectory": {
                            "llm_usage_summary": {
                                "calls": 1,
                                "input_tokens": 40,
                                "output_tokens": 10,
                                "cost_usd": 0.0004,
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = vault_svc.list_local_runs(root)
    check("list_local_runs finds one", len(rows) == 1)
    check("list row has tokens", rows[0].get("input_tokens") == 40)
    check("list row has wall duration", rows[0].get("duration_s") == 3.2)

    loaded = vault_svc.load_local_run(run_dir)
    check("load_local_run has usage", loaded["usage"]["input_tokens"] == 40)

    out = Path(td) / "export"
    dest = vault_svc.export_local_run(run_dir, out)
    check("export copies result.json", (dest / "result.json").is_file())

    r = runner.invoke(app, ["vault", "list", "--runs", str(root)])
    check("vault list exits 0", r.exit_code == 0, str(r.exit_code))
    check("vault list shows task", "demo-task" in r.stdout)

    r = runner.invoke(app, ["vault", "show", run_dir.name, "--runs", str(root)])
    check("vault show exits 0", r.exit_code == 0, str(r.exit_code))
    check("vault show prints usage", "in" in r.stdout.lower() or "40" in r.stdout)

    export_to = Path(td) / "cli-export"
    r = runner.invoke(
        app, ["vault", "export", run_dir.name, "--runs", str(root), "-o", str(export_to)]
    )
    check("vault export exits 0", r.exit_code == 0, str(r.exit_code))
    check("vault export wrote files", any(export_to.rglob("result.json")))

print("\n== agent status catalog ==")
cat = agent_status_catalog()
by_id = {a["id"]: a for a in cat}
check("catalog includes mini-swe verified", by_id.get("mini-swe", {}).get("status") == "verified")
check("catalog includes codex verified", by_id.get("codex", {}).get("status") == "verified")
check("removed agents not in catalog", "gemini" not in by_id and "openhands" not in by_id)

r = runner.invoke(app, ["--help"])
check("help lists vault", r.exit_code == 0 and "vault" in r.stdout)

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
