import csv
import json
from collections import defaultdict
from pathlib import Path


def expected():
    totals = defaultdict(int)
    with Path("/data/sales.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["status"] != "completed":
                continue
            totals[row["region"]] += int(row["amount"])
    regions = [{"region": r, "total": totals[r]} for r in sorted(totals)]
    return {"regions": regions, "grand_total": sum(totals.values())}


out_path = Path("/data/report.json")
assert out_path.exists(), "report.json was not created"
got = json.loads(out_path.read_text(encoding="utf-8"))
want = expected()
assert got == want, f"expected {want}, got {got}"
print("sales report ok")
