"""
Phase 1 fidelity suite — proves TraceTensor's Docker environment + verifier
behave as specified. Requires a running Docker daemon and the
python:3.11-slim image (auto-pulled on first run).

Run:  cd backend && python tests/test_phase1_fidelity.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from app.services.environment import DockerEnvironment  # noqa: E402
from app.services.verifier import _parse_reward, run_verifier  # noqa: E402

IMG = "python:3.11-slim"
passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def _mk_task():
    td = Path(tempfile.mkdtemp(prefix="tt_fid_"))
    (td / "environment").mkdir()
    (td / "tests").mkdir()
    (td / "environment" / "Dockerfile").write_text("FROM python:3.11-slim\nWORKDIR /app\n")
    (td / "tests" / "test.sh").write_text(
        "#!/bin/bash\nmkdir -p /logs/verifier\n"
        'if [ "$(cat /app/answer.txt 2>/dev/null)" = "42" ]; then '
        "echo '{\"reward\": 1.0}' > /logs/verifier/reward.json; "
        "else echo '{\"reward\": 0.0}' > /logs/verifier/reward.json; fi\n"
    )
    (td / "tests" / "Dockerfile").write_text(
        "FROM python:3.11-slim\nWORKDIR /app\nCOPY test.sh /tests/test.sh\n"
    )
    return td


print("== Reward rule (designated key is 'reward') ==")
check("reward.json {'reward':1} -> 1.0", _parse_reward(None, '{"reward":1.0}')[0] == 1.0)
check("reward.json {'score':0.5} alias -> 0.5", _parse_reward(None, '{"score":0.5}')[0] == 0.5)
check("reward.json single value -> that", _parse_reward(None, '{"acc":0.7}')[0] == 0.7)
check(
    "reward.json prefers 'reward' over others",
    _parse_reward(None, '{"score":0.0,"reward":1.0}')[0] == 1.0,
)
check("reward.txt '1' -> 1.0", _parse_reward("1", None)[0] == 1.0)

print("\n== Docker environment fidelity ==")


def probe(**kw):
    env = DockerEnvironment(Path("."), docker_image=IMG, **kw)
    try:
        env.setup()
    except Exception as e:
        return {"rejected": str(e)}
    try:
        return {
            "net": env.exec(
                "getent hosts example.com >/dev/null 2>&1 && echo Y || echo N"
            ).stdout.strip(),
            "mem": env.exec("cat /sys/fs/cgroup/memory.max 2>/dev/null || echo ?").stdout.strip(),
            "pwd": env.exec("pwd").stdout.strip(),
        }
    finally:
        env.teardown()


pub = probe(network_mode="public")
none = probe(network_mode="no-network")
res = probe(network_mode="no-network", memory_mb=256)
wd = probe(network_mode="no-network", workdir="/work")
allow = probe(network_mode="allowlist")
check("network public -> reachable", pub.get("net") == "Y")
check("network no-network -> blocked", none.get("net") == "N")
check("memory_mb=256 -> 256MB limit", res.get("mem") == str(256 * 1024 * 1024))
check("workdir honored", wd.get("pwd") == "/work")
check("allowlist -> rejected at setup", "rejected" in allow)

print("\n== Isolated verifier (environment_mode = separate) ==")
td = _mk_task()
TAMPER = (
    b"#!/bin/bash\nmkdir -p /logs/verifier\necho '{\"reward\": 1.0}' > /logs/verifier/reward.json\n"
)


def run_separate(answer):
    a = DockerEnvironment(td, network_mode="no-network")
    a.setup()
    a.exec("mkdir -p /tests", as_user="0")
    a.write_file("/app/answer.txt", answer.encode())
    a.write_file("/tests/test.sh", TAMPER)  # agent tampers its own test
    v = DockerEnvironment(td, dockerfile_dir="tests", network_mode="no-network")
    v.setup()
    v.transfer_from(a, ["/app/answer.txt"])
    r = run_verifier(v, td / "tests", copy_tests=False).reward  # baked-in real test
    a.teardown()
    v.teardown()
    return r


check("separate + correct answer -> 1.0", run_separate("42") == 1.0)
check("separate + wrong answer + tamper -> 0.0 (tamper ignored)", run_separate("WRONG") == 0.0)
shutil.rmtree(td, ignore_errors=True)

print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
