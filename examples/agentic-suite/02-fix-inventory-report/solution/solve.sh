#!/bin/bash
cat > /app/report.py << 'EOF'
"""Inventory value for configured warehouse."""

import csv
import json
from pathlib import Path


def total_value(data_dir: str = "/data") -> float:
    cfg = json.loads(Path(data_dir, "config.json").read_text())
    target = cfg.get("warehouse")
    total = 0.0
    with open(Path(data_dir, "inventory.csv"), newline="") as f:
        for row in csv.DictReader(f):
            if row["warehouse"] != target:
                continue
            total += float(row["quantity"]) * float(row["unit_price"])
    return round(total, 2)
EOF
