#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import csv
import json
from pathlib import Path

customers = {row["id"]: row["name"] for row in json.loads(Path("/data/customers.json").read_text())}
merged = []
with Path("/data/orders.csv").open(encoding="utf-8") as f:
    for row in csv.DictReader(f):
        cid = int(row["customer_id"])
        if cid not in customers:
            continue
        merged.append(
            {
                "order_id": int(row["order_id"]),
                "customer_id": cid,
                "customer_name": customers[cid],
                "amount": int(row["amount"]),
            }
        )
merged.sort(key=lambda r: r["order_id"])
Path("/data/merged.json").write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
PY
