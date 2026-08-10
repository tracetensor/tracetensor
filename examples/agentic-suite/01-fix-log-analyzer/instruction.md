# Fix log analyzer — staging metrics wrong

## PR description

The service ships a small log analysis helper. Sample logs live under `/data/logs/`.
The module `/app/analyzer.py` exposes `count_levels(paths)` which should return a
dict counting **ERROR**, **WARN**, and **INFO** lines across all given files.

Log line format: `[LEVEL] message` where LEVEL is exactly one of ERROR, WARN, INFO.

Current counts are wrong in production. Fix `/app/analyzer.py` only.
Do not change files under `/data/` or the tests.

Expected on the bundled sample logs:
- ERROR: 2
- WARN: 1
- INFO: 3
