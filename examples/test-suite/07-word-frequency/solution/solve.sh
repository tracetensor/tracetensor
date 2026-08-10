#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
import re
from collections import Counter
from pathlib import Path

text = Path("/data/sample.txt").read_text(encoding="utf-8").lower()
words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
counts = Counter(words)
ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
Path("/data/top_words.json").write_text(
    json.dumps({"words": ranked[:5]}, indent=2) + "\n",
    encoding="utf-8",
)
PY
