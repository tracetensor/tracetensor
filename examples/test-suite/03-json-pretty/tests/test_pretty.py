import json
from pathlib import Path


def _sorted(obj):
    if isinstance(obj, dict):
        return {k: _sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_sorted(x) for x in obj]
    return obj


src = json.loads(Path("/data/minified.json").read_text(encoding="utf-8"))
out_path = Path("/data/pretty.json")
assert out_path.exists(), "pretty.json was not created"
text = out_path.read_text(encoding="utf-8")
parsed = json.loads(text)
assert parsed == src, "parsed content must match input"
# Format: 2-space indent, keys sorted; trailing newline optional.
normalized = json.dumps(_sorted(parsed), indent=2, sort_keys=True)
got_normalized = json.dumps(_sorted(json.loads(text.strip())), indent=2, sort_keys=True)
assert got_normalized == normalized, f"format mismatch:\n{text!r}"
print("pretty json ok")
