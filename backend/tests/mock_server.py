"""
Stdlib-only HTTP server serving the real Stage-1 endpoints, backed by the REAL
parser/validator/storage core plus an in-memory task store. Verifies the UI
end-to-end offline (no FastAPI/Postgres). Uses a hand-rolled multipart parser
(email.parser) to avoid the deprecated, hang-prone cgi module.
"""

import email
import io
import json
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE.parent))

import _pydantic_shim  # noqa: E402

_pydantic_shim.install()

from app.services.task_parser import parse_task_toml  # noqa: E402
from app.services.task_validator import STATUS_READY, validate_task  # noqa: E402
from app.storage.task_store import (  # noqa: E402
    read_task_file,
    store_task_from_files,
    store_task_from_zip,
)

TASKS_ROOT = Path("/tmp/tt_tasks")
TASKS_ROOT.mkdir(parents=True, exist_ok=True)
FRONTEND = PROJECT / "frontend"
DB = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def default_toml(name):
    return (
        f'schema_version = "1.3"\n\n[task]\nname = "{name}"\n\n'
        "[verifier]\ntimeout_sec = 120.0\n\n[agent]\ntimeout_sec = 120.0\n\n"
        '[environment]\nnetwork_mode = "public"\nbuild_timeout_sec = 600.0\n'
    ).encode()


def parse_multipart(body, content_type):
    """Return {field_name: (filename_or_None, bytes)} using email.parser."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = email.message_from_bytes(header + body)
    out = {}
    if not msg.is_multipart():
        return out
    for part in msg.get_payload():
        cd = part.get("Content-Disposition", "")
        if "form-data" not in cd:
            continue
        name = part.get_param("name", header="Content-Disposition")
        filename = part.get_param("filename", header="Content-Disposition")
        payload = part.get_payload(decode=True) or b""
        out[name] = (filename, payload)
    return out


def persist(task_dir):
    v = validate_task(task_dir)
    cfg = parse_task_toml((task_dir / "task.toml").read_bytes(), task_dir)
    for tid, rec in list(DB.items()):
        if rec["name"] == cfg.name:
            del DB[tid]
    tid = str(uuid.uuid4())
    env_image = cfg.environment.docker_image or ("Dockerfile" if v.has_dockerfile else None)
    DB[tid] = {
        "id": tid,
        "name": cfg.name,
        "description": cfg.description,
        "task_type": "coding",
        "config": cfg.model_dump(),
        "task_dir": str(task_dir),
        "category": cfg.category,
        "status": v.status,
        "created_at": now(),
        "environment_os": cfg.environment.os,
        "network_mode": cfg.environment.network_mode,
        "environment_image": env_image,
        "timeout_agent_sec": cfg.agent.timeout_sec or 120.0,
        "timeout_verifier_sec": cfg.verifier.timeout_sec,
        "v": v,
    }
    return tid


def registration(tid):
    r = DB[tid]
    v = r["v"]
    cfg = r["config"]
    ready = r["status"] == STATUS_READY
    return {
        "id": tid,
        "status": r["status"],
        "message": (
            "Patient registered. All paperwork validated — ready for examination."
            if ready
            else "Patient registered, but paperwork is incomplete. See validation errors."
        ),
        "patient_chart": {
            "name": r["name"],
            "type": "coding",
            "description": r["description"],
            "category": r["category"],
            "authors": cfg.get("authors", []),
            "keywords": cfg.get("keywords", []),
        },
        "examination_room": {
            "environment": r["environment_image"] or "Dockerfile",
            "os": r["environment_os"],
            "network_mode": r["network_mode"],
            "agent_timeout_sec": r["timeout_agent_sec"],
            "verifier_timeout_sec": r["timeout_verifier_sec"],
        },
        "health_checks": {
            "has_instruction": v.has_instruction,
            "has_task_toml": v.has_task_toml,
            "has_dockerfile": v.has_dockerfile,
            "has_docker_image": v.has_docker_image,
            "has_test_script": v.has_test_script,
            "has_solution": v.has_solution,
        },
        "validation": {"errors": v.errors, "warnings": v.warnings},
        "next_step": (
            f"POST /examine/{tid} to begin Stage 2 (Examination Room)."
            if ready
            else "Fix the reported errors and re-upload."
        ),
        "created_at": r["created_at"],
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        p = self.path
        if p in ("/", "/index.html"):
            return self._send(200, (FRONTEND / "index.html").read_bytes(), "text/html")
        if p == "/api/health":
            return self._send(200, {"status": "ok"})
        if p == "/ingest/tasks":
            tasks = sorted(DB.values(), key=lambda r: r["created_at"], reverse=True)
            return self._send(
                200,
                {
                    "count": len(tasks),
                    "tasks": [
                        {
                            "id": r["id"],
                            "name": r["name"],
                            "description": r["description"],
                            "task_type": r["task_type"],
                            "status": r["status"],
                            "category": r["category"],
                            "environment_os": r["environment_os"],
                            "created_at": r["created_at"],
                        }
                        for r in tasks
                    ],
                },
            )
        if p.startswith("/ingest/task/") and p.endswith("/files"):
            tid = p.split("/")[3]
            if tid not in DB:
                return self._send(404, {"detail": "Task not found."})
            d = Path(DB[tid]["task_dir"])
            return self._send(
                200,
                {
                    "instruction_md": read_task_file(d, "instruction.md"),
                    "task_toml": read_task_file(d, "task.toml"),
                    "dockerfile": read_task_file(d, "environment/Dockerfile"),
                    "test_sh": read_task_file(d, "tests/test.sh"),
                    "solve_sh": read_task_file(d, "solution/solve.sh"),
                },
            )
        if p.startswith("/ingest/task/"):
            tid = p.split("/")[3]
            if tid not in DB:
                return self._send(404, {"detail": "Task not found."})
            DB[tid]["v"] = validate_task(Path(DB[tid]["task_dir"]))
            DB[tid]["status"] = DB[tid]["v"].status
            return self._send(200, registration(tid))
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        try:
            body = self._read_body()
            fields = parse_multipart(body, self.headers.get("Content-Type", ""))
            if self.path == "/ingest/task/upload":
                fn, data = fields.get("file", (None, b""))
                if not fn:
                    return self._send(400, {"detail": "Please upload a .zip file."})
                name = Path(fn).stem
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        for m in zf.namelist():
                            if m.endswith("task.toml"):
                                name = parse_task_toml(zf.read(m)).name
                                break
                except Exception:
                    pass
                try:
                    d = store_task_from_zip(TASKS_ROOT, name, data)
                except ValueError as e:
                    return self._send(400, {"detail": f"Bad archive: {e}"})
                if not (d / "task.toml").exists():
                    (d / "task.toml").write_bytes(default_toml(name))
                return self._send(200, registration(persist(d)))

            if self.path == "/ingest/task/create":
                name = (fields.get("name", (None, b""))[1] or b"").decode().strip()
                if not name:
                    return self._send(400, {"detail": "Task name is required."})
                files = {}
                mapping = {
                    "instruction": "instruction.md",
                    "dockerfile": "environment/Dockerfile",
                    "test_script": "tests/test.sh",
                    "solution": "solution/solve.sh",
                    "task_config": "task.toml",
                }
                for field, path in mapping.items():
                    fn, data = fields.get(field, (None, None))
                    if fn:
                        files[path] = data
                if "task.toml" not in files:
                    files["task.toml"] = default_toml(name)
                d = store_task_from_files(TASKS_ROOT, name, files)
                return self._send(200, registration(persist(d)))

            return self._send(404, {"detail": "not found"})
        except Exception as e:
            import traceback

            traceback.print_exc()
            return self._send(500, {"detail": f"server error: {e}"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"mock server on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
