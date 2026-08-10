import sys

sys.path.insert(0, "/app")
from manifest import list_files  # noqa: E402

got = list_files("/data/project")
want = ["README.md", "src/app.py", "src/util.py"]
assert got == want, f"got {got}, want {want}"
print("manifest ok")
