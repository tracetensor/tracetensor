#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
from pathlib import Path

src = json.loads(Path("/data/minified.json").read_text(encoding="utf-8"))
Path("/data/pretty.json").write_text(
    json.dumps(src, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
