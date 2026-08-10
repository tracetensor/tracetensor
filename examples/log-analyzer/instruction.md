# Access Log Analyzer

You are given an Nginx access log at `/data/access.log` in the standard
combined log format, e.g.:

```
192.168.1.10 - - [10/Oct/2024:13:55:36 +0000] "GET /api/users HTTP/1.1" 200 1534 "-" "curl/7.68"
```

Write a program that analyzes the log and writes a JSON report to
`/data/report.json` with exactly these keys:

- `total_requests` (int): total number of log lines
- `unique_ips` (int): number of distinct client IP addresses
- `status_counts` (object): map of HTTP status code (as string) to count,
  e.g. `{"200": 40, "404": 5}`
- `top_path` (string): the request path that appears most often
- `error_rate` (float): fraction of requests with status >= 400, rounded to
  3 decimal places

The report must be valid JSON. Only these five keys, spelled exactly as above.
