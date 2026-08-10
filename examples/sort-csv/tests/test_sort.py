import csv
import sys

try:
    with open("/data/sorted_sales.csv") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 5, f"Expected 5 rows, got {len(rows)}"
    assert rows[0]["company"] == "Beta", "First row should be Beta (98000)"
    assert rows[-1]["company"] == "Delta", "Last row should be Delta (23000)"

    revenues = [int(r["revenue"]) for r in rows]
    assert revenues == sorted(revenues, reverse=True), "Not sorted descending"

    print("All checks passed")
    sys.exit(0)
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)
