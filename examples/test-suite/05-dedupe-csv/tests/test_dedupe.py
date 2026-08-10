import csv
from pathlib import Path


def expected_rows():
    seen = set()
    rows = []
    with Path("/data/users.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row["email"]
            if email in seen:
                continue
            seen.add(email)
            rows.append(row)
    return rows


out = Path("/data/users_unique.csv")
assert out.exists(), "users_unique.csv was not created"
with out.open(encoding="utf-8") as f:
    got = list(csv.DictReader(f))
want = expected_rows()
assert got == want, f"expected {want}, got {got}"
print("dedupe ok")
