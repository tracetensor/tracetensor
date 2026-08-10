A CSV file is at `/data/sales.csv` with columns:

`region,amount,status,date`

Build a sales report from **completed** rows only:

1. Sum `amount` by `region`
2. Sort regions alphabetically
3. Compute `grand_total` across those sums

Write `/data/report.json` with exactly:

```json
{
  "regions": [
    {"region": "east", "total": 120},
    {"region": "north", "total": 100},
    ...
  ],
  "grand_total": 465
}
```

Use integers for all totals. Include only the two top-level keys shown above.
