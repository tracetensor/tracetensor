# Fix inventory report — warehouse totals wrong

## PR description

`/app/report.py` reads `/data/inventory.csv` and `/data/config.json`, then
returns `total_value()` = sum of (quantity × unit_price) for rows whose
`warehouse` matches `config.json` key `"warehouse"`.

CSV columns: `sku,warehouse,quantity,unit_price`

Fix `/app/report.py`. Do not modify `/data/*` or tests.

Bundled data: warehouse `"east"` → total value **125.0**
