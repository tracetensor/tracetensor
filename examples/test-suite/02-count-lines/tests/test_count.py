from pathlib import Path

expected = len(Path("/data/notes.txt").read_text(encoding="utf-8").splitlines())
out = Path("/data/line_count.txt")
assert out.exists(), "line_count.txt was not created"
got = out.read_text(encoding="utf-8").strip()
assert got == str(expected), f"expected {expected}, got {got!r}"
print("line count ok")
