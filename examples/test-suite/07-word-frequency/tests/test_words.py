import json
import re
from collections import Counter
from pathlib import Path


def expected():
    text = Path("/data/sample.txt").read_text(encoding="utf-8").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    counts = Counter(words)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    # JSON has no tuples — expect lists of [word, count].
    return {"words": [[w, c] for w, c in ranked[:5]]}


out_path = Path("/data/top_words.json")
assert out_path.exists(), "top_words.json was not created"
got = json.loads(out_path.read_text(encoding="utf-8"))
want = expected()
assert got == want, f"expected {want}, got {got}"
print("word frequency ok")
