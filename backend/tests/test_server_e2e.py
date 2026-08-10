"""
End-to-end test of the mock server: starts it in a thread, hits every endpoint
with urllib, asserts the real core logic produces correct results. Fully
self-contained in one process so it survives a single bash invocation.
"""

import io
import json
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1]))

import importlib.util

spec = importlib.util.spec_from_file_location("mock_server", HERE.parent / "mock_server.py")
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)

PORT = 8137
srv = ms.ThreadingHTTPServer(("127.0.0.1", PORT), ms.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)
BASE = f"http://127.0.0.1:{PORT}"

passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read())


def post_zip(path, name, zbytes):
    boundary = "----tt"
    body = b""
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode()
    body += b"Content-Type: application/zip\r\n\r\n" + zbytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
    return buf.getvalue()


print("\n== health ==")
st, d = get("/api/health")
check("health ok", st == 200 and d["status"] == "ok")

print("\n== empty list ==")
st, d = get("/ingest/tasks")
check("count 0", d["count"] == 0)

print("\n== upload real task example ==")
PROJECT = HERE.parents[2]
example = PROJECT / "examples" / "sort-csv"
files = {}
for f in example.rglob("*"):
    if f.is_file():
        files[str(f.relative_to(example.parent))] = f.read_bytes()  # keep sort-csv/ prefix
st, reg = post_zip("/ingest/task/upload", "sort-csv.zip", make_zip(files))
check("upload 200", st == 200)
check("name correct", reg["patient_chart"]["name"] == "tracetensor/sort-csv")
check("status ready", reg["status"] == "ready_for_examination")
check(
    "all health checks green",
    all(
        [
            reg["health_checks"]["has_instruction"],
            reg["health_checks"]["has_dockerfile"],
            reg["health_checks"]["has_test_script"],
            reg["health_checks"]["has_solution"],
        ]
    ),
)
check("no errors", reg["validation"]["errors"] == [])
check("tests timeout 60", reg["examination_room"]["verifier_timeout_sec"] == 60.0)
tid = reg["id"]

print("\n== list now has 1 ==")
st, d = get("/ingest/tasks")
check("count 1", d["count"] == 1)

print("\n== fetch detail ==")
st, d = get(f"/ingest/task/{tid}")
check("detail name", d["patient_chart"]["name"] == "tracetensor/sort-csv")

print("\n== fetch files ==")
st, d = get(f"/ingest/task/{tid}/files")
check("instruction present", "revenue" in (d["instruction_md"] or ""))
check("dockerfile present", "python" in (d["dockerfile"] or "").lower())
check("test present", d["test_sh"] is not None)
check("solve present", d["solve_sh"] is not None)

print("\n== broken evaluation (no test, no solution) ==")
broken = make_zip(
    {
        "broken/instruction.md": "do a thing",
        "broken/task.toml": '[task]\nname="test/broken"\n',
        "broken/environment/Dockerfile": "FROM python:3.11",
    }
)
st, reg = post_zip("/ingest/task/upload", "broken.zip", broken)
check("broken not ready", reg["status"] != "ready_for_examination")
check(
    "reports missing tests",
    any("test.sh" in e or "tests" in e for e in reg["validation"]["errors"]),
)
check("warns missing solution", any("solve.sh" in w for w in reg["validation"]["warnings"]))

print("\n== 404 for unknown id ==")
try:
    get("/ingest/task/does-not-exist")
    check("404 raised", False)
except urllib.error.HTTPError as e:
    check("404 raised", e.code == 404)

srv.shutdown()
print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
