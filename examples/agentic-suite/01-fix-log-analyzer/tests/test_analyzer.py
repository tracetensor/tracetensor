import sys

sys.path.insert(0, "/app")
from analyzer import count_levels  # noqa: E402

paths = ["/data/logs/app.log", "/data/logs/app2.log"]
got = count_levels(paths)
want = {"ERROR": 2, "WARN": 1, "INFO": 3}
assert got == want, f"got {got}, want {want}"
print("log analyzer ok")
