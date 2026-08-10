#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import csv
import json
from collections import defaultdict
from pathlib import Path

totals = defaultdict(int)
with Path("/data/sales.csv").open(encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["status"] != "completed":
            continue
        totals[row["region"]] += int(row["amount"])
report = {
    "regions": [{"region": r, "total": totals[r]} for r in sorted(totals)],
    "grand_total": sum(totals.values()),
}
Path("/data/report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
PY
