"""Broken inventory report — ignores warehouse filter."""

import csv
import json
from pathlib import Path


def total_value(data_dir: str = "/data") -> float:
    cfg = json.loads(Path(data_dir, "config.json").read_text())
    target = cfg.get("warehouse")
    total = 0.0
    with open(Path(data_dir, "inventory.csv"), newline="") as f:
        for row in csv.DictReader(f):
            # BUG: sums all rows, ignores warehouse
            total += float(row["quantity"]) * float(row["unit_price"])
    return round(total, 2)
