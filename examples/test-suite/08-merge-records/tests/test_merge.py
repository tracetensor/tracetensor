import csv
import json
from pathlib import Path


def expected():
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
    return merged


out_path = Path("/data/merged.json")
assert out_path.exists(), "merged.json was not created"
got = json.loads(out_path.read_text(encoding="utf-8"))
want = expected()
assert got == want, f"expected {want}, got {got}"
print("merge ok")
