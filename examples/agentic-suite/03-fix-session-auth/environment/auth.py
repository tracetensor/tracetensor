"""Broken session resolver — returns user id string instead of user dict."""

import json
from pathlib import Path


def resolve_user(session_id: str, data_dir: str = "/data") -> dict | None:
    sessions = json.loads(Path(data_dir, "sessions.json").read_text())
    users = json.loads(Path(data_dir, "users.json").read_text())
    uid = sessions.get(session_id)
    if not uid:
        return None
    # BUG: returns uid string, not users[uid]
    return uid  # type: ignore[return-value]
