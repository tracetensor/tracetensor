# Fix query filter — wrong AND/OR precedence

## PR description

`matches(query, record)` in `/app/query.py` evaluates a tiny filter language on
a record dict (string field → string value).

Syntax: clauses joined by `AND` or `OR` (uppercase). **`AND` binds tighter than
`OR`** (standard boolean precedence).

Clause format: `field==value` (exact match on `record[field]`).

Fix `/app/query.py`. Do not change tests.

### Example

Query: `status==open AND owner==alice OR owner==bob`

Means: `(status==open AND owner==alice) OR owner==bob`

Record `{"status": "open", "owner": "bob"}` → **True** (second OR branch).
