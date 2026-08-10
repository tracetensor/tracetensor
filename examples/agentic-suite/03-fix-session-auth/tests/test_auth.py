import sys

sys.path.insert(0, "/app")
from auth import resolve_user  # noqa: E402

assert resolve_user("sess-7") == {"id": "u2", "role": "admin"}
assert resolve_user("sess-9") is None
assert resolve_user("missing") is None
print("session auth ok")
