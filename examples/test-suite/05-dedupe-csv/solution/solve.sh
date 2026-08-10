#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import csv
from pathlib import Path

seen = set()
rows = []
with Path("/data/users.csv").open(encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    for row in reader:
        email = row["email"]
        if email in seen:
            continue
        seen.add(email)
        rows.append(row)
with Path("/data/users_unique.csv").open("w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
PY
