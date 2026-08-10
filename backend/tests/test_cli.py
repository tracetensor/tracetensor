"""
CLI suite — the `tracetensor` command. Offline checks always run (version, help,
task validation, error paths) via Typer's in-process runner; the real `run`
command is smoke-tested only when Docker is available (it executes a container).

Run:  cd backend && python tests/test_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
PROJECT = HERE.parents[2]
sys.path.insert(0, str(BACKEND))

from typer.testing import CliRunner  # noqa: E402

from app.cli.main import app  # noqa: E402

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


def docker_ok() -> bool:
    return bool(shutil.which("docker")) and (
        subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


SORT_CSV = PROJECT / "examples" / "sort-csv"

print("== offline: version / help / validate ==")
r = runner.invoke(app, ["version"])
check("version exits 0", r.exit_code == 0, str(r.exit_code))
check("version prints the wordmark + number", "TraceTensor" in r.stdout and "0.1.0" in r.stdout)

r = runner.invoke(app, ["--help"])
check(
    "--help exits 0 and lists commands",
    r.exit_code == 0 and "run" in r.stdout and "serve" in r.stdout and "vault" in r.stdout,
)

r = runner.invoke(app, ["tasks", "validate", str(SORT_CSV)])
check("validate a good task -> exit 0", r.exit_code == 0, str(r.exit_code))
check("validate says ready", "ready" in r.stdout.lower())

r = runner.invoke(app, ["tasks", "validate", "/no/such/path"])
check("validate a missing path -> exit 2", r.exit_code == 2, str(r.exit_code))

r = runner.invoke(app, ["run", "/no/such/path", "-a", "oracle"])
check("run on a non-task -> exit 2 (clean error, no traceback)", r.exit_code == 2, str(r.exit_code))

print("\n== offline: tasks init ==")

with tempfile.TemporaryDirectory() as td:
    dest = Path(td) / "scaffold-eval"
    r = runner.invoke(app, ["tasks", "init", str(dest)])
    check("tasks init exits 0", r.exit_code == 0, str(r.exit_code))
    check("tasks init wrote task.toml", (dest / "task.toml").is_file())
    r = runner.invoke(app, ["tasks", "validate", str(dest)])
    check("scaffolded eval validates", r.exit_code == 0, str(r.exit_code))

print("\n== offline: agent factory / registry ==")
from app.services.agents import make_agent  # noqa: E402

_td = Path("/tmp")
check("oracle -> OracleAgent", type(make_agent("oracle", _td)).__name__ == "OracleAgent")
check(
    "mini-swe -> MiniSweAgent",
    type(make_agent("mini-swe", _td, "anthropic/claude-haiku-4-5")).__name__ == "MiniSweAgent",
)
check("mini alias -> MiniSweAgent", type(make_agent("mini", _td)).__name__ == "MiniSweAgent")
check(
    "provider name -> LLMAgent (built-in bash loop)",
    type(make_agent("anthropic", _td)).__name__ == "LLMAgent",
)
_m = make_agent("mini-swe", _td, "claude-haiku-4-5")  # bare model → assume anthropic
check("mini-swe normalizes a bare model to a litellm id", _m.model == "anthropic/claude-haiku-4-5")
try:
    make_agent("definitely-not-an-agent", _td)
    check("unknown agent raises", False)
except ValueError:
    check("unknown agent raises", True)

from app.services.agents.mini_swe import _llm_calls_from_traj  # noqa: E402

_traj = {"info": {"model_stats": {"instance_cost": 0.0661, "api_calls": 11}}}
_calls = _llm_calls_from_traj(_traj, "anthropic", "anthropic/claude-haiku-4-5")
check(
    "installed-agent cost parsed from trajectory (vendor-reported)",
    len(_calls) == 1 and _calls[0]["cost_usd"] == 0.0661 and _calls[0]["api_calls"] == 11,
    str(_calls[:1])[:120],
)
check(
    "a trajectory with no model_stats yields no cost records (not a fake zero)",
    _llm_calls_from_traj({"messages": []}, "anthropic", "m") == []
    and _llm_calls_from_traj("garbled", "anthropic", "m") == [],
)
from app.services.trial_runner import _summarize_llm_usage  # noqa: E402

_summary = _summarize_llm_usage(_calls)
check(
    "cost summary reflects api_calls count + vendor cost",
    _summary["calls"] == 11 and _summary["cost_usd"] == 0.0661,
    str(_summary),
)

print("\n== offline: claude-code agent ==")
from app.services.agents import AgentConfigError, resolve_agent  # noqa: E402
from app.services.agents.claude_code import (  # noqa: E402
    ClaudeCodeAgent,
    _extract_json_object,
    _llm_calls_from_claude_result,
)

check(
    "claude-code -> ClaudeCodeAgent",
    type(make_agent("claude-code", _td)).__name__ == "ClaudeCodeAgent",
)
check("cc alias -> ClaudeCodeAgent", type(make_agent("cc", _td)).__name__ == "ClaudeCodeAgent")
check("claude-code defaults to the haiku alias", ClaudeCodeAgent(None).model == "haiku")
check(
    "claude-code strips a litellm 'anthropic/' prefix for --model",
    ClaudeCodeAgent("anthropic/claude-haiku-4-5").model == "claude-haiku-4-5",
)

# Claude Code prints its result JSON (cost top-level, num_turns) to stdout.
_ccjson = (
    '{"type":"result","subtype":"success","total_cost_usd":0.031,"num_turns":7,"result":"done"}'
)
_ccraw = _extract_json_object("some startup notice\n" + _ccjson)
check("claude-code result JSON is recovered even after a leading notice", _ccraw is not None)
_cccalls = _llm_calls_from_claude_result(_ccraw, "haiku")
check(
    "claude-code turns parsed + vendor cost preserved",
    len(_cccalls) == 1 and _cccalls[0]["cost_usd"] == 0.031 and _cccalls[0]["api_calls"] == 7,
    str(_cccalls[:1])[:120],
)
check(
    "empty / non-JSON stdout yields no fake cost record",
    _extract_json_object("") is None and _llm_calls_from_claude_result(None, "haiku") == [],
)

print("\n== offline: codex agent ==")
from app.services.agents.codex import (  # noqa: E402
    CodexAgent,
    _llm_calls_from_codex_jsonl,
    _parse_jsonl,
)

check("codex -> CodexAgent", type(make_agent("codex", _td)).__name__ == "CodexAgent")

check(
    "codex strips a litellm 'openai/' prefix",
    CodexAgent("openai/gpt-5-codex").model == "gpt-5-codex",
)

# Codex JSONL: count turns (no USD, ambiguous tokens → recorded as unknown).
_codex_out = '{"type":"thread.started"}\n{"type":"turn.completed","usage":{"input_tokens":10}}\n{"type":"turn.completed"}'
check("codex turn count parsed from JSONL", len(_parse_jsonl(_codex_out)) == 3)
_cx = _llm_calls_from_codex_jsonl(_codex_out, "gpt-5-codex")
check(
    "codex records api_calls (turns) with cost/tokens unknown",
    len(_cx) == 1 and _cx[0]["api_calls"] == 2 and _cx[0]["cost_usd"] is None,
    str(_cx[:1])[:120],
)

print("\n== offline: shared agent resolver (API gate) ==")


class _FakeSettings:
    """Minimal stand-in for app Settings — just the attrs resolve_agent reads."""

    def __init__(self, **on):
        self.ANTHROPIC_API_KEY = "x" if on.get("anthropic") else None
        self.OPENAI_API_KEY = "x" if on.get("openai") else None
        self.OPENROUTER_API_KEY = "x" if on.get("openrouter") else None

    def available_providers(self):
        return {
            "anthropic": bool(self.ANTHROPIC_API_KEY),
            "openai": bool(self.OPENAI_API_KEY),
            "openrouter": bool(self.OPENROUTER_API_KEY),
        }


_all = _FakeSettings(anthropic=True, openai=True)
_anth = _FakeSettings(anthropic=True)
check("resolve oracle -> (oracle, None)", resolve_agent("oracle", None, _anth) == ("oracle", None))
check(
    "resolve 'mini' alias -> stored label 'mini-swe' + default model",
    resolve_agent("mini", None, _anth) == ("mini-swe", "anthropic/claude-haiku-4-5"),
)
check(
    "resolve 'cc' alias -> stored label 'claude-code' + default model",
    resolve_agent("cc", None, _anth) == ("claude-code", "haiku"),
)
check("resolve codex needs OPENAI key", resolve_agent("codex", None, _all)[0] == "codex")
check(
    "resolve provider name -> canonical provider + its default model",
    resolve_agent("anthropic", None, _anth)[0] == "anthropic",
)
try:
    resolve_agent("codex", None, _anth)  # only anthropic configured
    check("resolver raises when codex's key is missing", False)
except AgentConfigError:
    check("resolver raises when codex's key is missing", True)
try:
    resolve_agent("openai", None, _anth)  # key not available
    check("resolver raises when the provider key is missing", False)
except AgentConfigError:
    check("resolver raises when the provider key is missing", True)
try:
    resolve_agent("mini", None, _FakeSettings())  # installed agent, key missing
    check("resolver raises when an installed agent's model key is missing", False)
except AgentConfigError:
    check("resolver raises when an installed agent's model key is missing", True)
try:
    resolve_agent("definitely-not-an-agent", None, _anth)
    check("resolver raises on an unknown agent", False)
except AgentConfigError:
    check("resolver raises on an unknown agent", True)

print("\n== docker: real `run` (oracle, --json) ==")
if docker_ok():
    r = runner.invoke(app, ["run", str(SORT_CSV), "-a", "oracle", "-n", "1", "--json", "--no-save"])
    check("run exits 0 (something passed)", r.exit_code == 0, str(r.exit_code))
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {}
    check("emits valid JSON", bool(data), r.stdout[:120])
    check("reports the task", data.get("task") == "tracetensor/sort-csv", str(data.get("task")))
    check(
        "oracle passed with reward 1.0",
        data.get("passed") == 1 and data.get("mean_reward") == 1.0,
        f"passed={data.get('passed')} reward={data.get('mean_reward')}",
    )

    print("\n== docker: `run --server` lands in the server's DB (dashboard) ==")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    tmp = Path(tempfile.mkdtemp(prefix="tt_cli_srv_"))
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'c.db'}",
        "TASKS_ROOT": str(tmp / "tasks"),
        # Explicit: conftest sets WORKER_EMBEDDED=false for the pytest process
        # and this subprocess inherits its environment. Without it the server
        # has no worker, the job never executes, and the test waits out its
        # timeout instead of failing with a reason.
        "WORKER_EMBEDDED": "true",
    }
    (tmp / "tasks").mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/api/health", timeout=5)
                break
            except Exception:
                time.sleep(0.5)
        r = runner.invoke(
            app, ["run", str(SORT_CSV), "-a", "oracle", "-n", "1", "--server", base, "--json"]
        )
        check("run --server exits 0", r.exit_code == 0, str(r.exit_code))
        rd = {}
        try:
            rd = json.loads(r.stdout)
        except Exception:
            pass
        check(
            "remote run passed + returns a dashboard link",
            rd.get("passed") == 1 and bool(rd.get("job_url")),
            str(rd)[:120],
        )
        with urllib.request.urlopen(f"{base}/v1/examine/jobs", timeout=10) as resp:
            page = json.loads(resp.read())
        jobs = page["items"]
        check(
            "the CLI run is now in the server's job list",
            len(jobs) == 1 and page["total"] == 1,
            f"{len(jobs)} jobs, total={page.get('total')}",
        )
        check(
            "that job is the oracle run, completed",
            jobs and jobs[0]["agent"] == "oracle" and jobs[0]["status"] == "completed",
            str(jobs[:1])[:120],
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("  (skipped — Docker not available)")

print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
