#!/bin/bash
set -e
python3 - <<'PY'
import csv
with open("/data/sales.csv") as f:
    rows = sorted(csv.DictReader(f), key=lambda r: int(r["revenue"]), reverse=True)
with open("/data/sorted_sales.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["company", "product", "revenue"])
    w.writeheader()
    w.writerows(rows)
PY
