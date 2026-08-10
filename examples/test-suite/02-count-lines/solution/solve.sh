#!/bin/bash
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
count = len(Path("/data/notes.txt").read_text(encoding="utf-8").splitlines())
Path("/data/line_count.txt").write_text(str(count), encoding="utf-8")
PY
