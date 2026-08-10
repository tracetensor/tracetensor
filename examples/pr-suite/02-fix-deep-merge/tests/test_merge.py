import sys

sys.path.insert(0, "/app")
from merge import deep_merge  # noqa: E402

base = {"a": {"x": 1, "z": 9}, "k": 0}
patch = {"a": {"y": 2}, "b": 3}
got = deep_merge(base, patch)
want = {"a": {"x": 1, "y": 2, "z": 9}, "k": 0, "b": 3}
assert got == want, f"got {got}, want {want}"
assert base == {"a": {"x": 1, "z": 9}, "k": 0}, "base mutated"

got2 = deep_merge({"a": 1}, {"a": 2})
assert got2 == {"a": 2}

print("deep_merge ok")
