"""
Thin HTTP client for `tracetensor run --server <url>` — submit a run to a running
`tracetensor serve` instance so it lands in the shared history + leaderboard and
executes on the server's durable queue (and its worker fleet).

Stdlib only (urllib) — the CLI shouldn't drag in a heavy HTTP dependency. Talks
to the versioned `/v1` API and honours API_TOKEN via a Bearer header.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional


class ServerError(Exception):
    """A remote call failed (unreachable, auth, or a 4xx/5xx)."""


def _auth(token: Optional[str], extra: Optional[dict] = None) -> dict:
    h = dict(extra or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _req(
    method: str,
    url: str,
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    timeout: float = 60.0,
) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("detail", "")
        except Exception:
            pass
        if e.code == 401:
            raise ServerError("unauthorized — pass --token (or set TRACETENSOR_TOKEN)") from e
        raise ServerError(f"server returned {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ServerError(f"cannot reach {url} ({e.reason})") from e


def check_health(base: str, token: Optional[str] = None, timeout: float = 5.0) -> None:
    """Raise ServerError if the server is unreachable or unhealthy."""
    _req("GET", f"{base}/api/health", None, _auth(token), timeout=timeout)


def zip_task(task_dir: Path) -> bytes:
    """Zip a task directory flat (files at the archive root)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(task_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(task_dir).as_posix())
    return buf.getvalue()


def upload_task(base: str, task_dir: Path, token: Optional[str]) -> str:
    """Upload the task; returns its server-side task id."""
    boundary = "----tracetensorCLIboundary"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="task.zip"\r\n',
            b"Content-Type: application/zip\r\n\r\n",
            zip_task(task_dir),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    headers = _auth(token, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    data = _req("POST", f"{base}/v1/ingest/task/upload", body, headers, timeout=120.0)
    return _require_id(data, "task upload")


def _require_id(data: object, what: str) -> str:
    """Pull an `id` out of a server response, or fail with what went wrong.

    The response is untyped JSON from a server we don't control the version of.
    `data["id"]` would raise KeyError/TypeError deep in the CLI; this says which
    call came back malformed.
    """
    job_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(job_id, str):
        raise ServerError(f"Unexpected response from {what}: no string `id` field.")
    return job_id


def start_examination(base: str, task_id: str, req: dict, token: Optional[str]) -> str:
    """Enqueue a run on the server; returns its job id."""
    headers = _auth(token, {"Content-Type": "application/json"})
    data = _req("POST", f"{base}/v1/examine/{task_id}", json.dumps(req).encode(), headers)
    return _require_id(data, "examination start")


def get_job(base: str, job_id: str, token: Optional[str]) -> dict:
    return _req("GET", f"{base}/v1/examine/job/{job_id}", None, _auth(token))


def list_jobs(base: str, token: Optional[str]) -> list:
    """Jobs from a running server.

    List endpoints return a page envelope ({items, total, limit, offset}). The
    bare-list branch is kept so this CLI still talks to a server running an
    older build rather than silently showing an empty Vault.
    """
    data = _req("GET", f"{base}/v1/vault/jobs", None, _auth(token))
    if isinstance(data, dict):
        items = data.get("items")
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def export_job(base: str, job_id: str, token: Optional[str]) -> dict:
    return _req("GET", f"{base}/v1/vault/export/{job_id}", None, _auth(token))
