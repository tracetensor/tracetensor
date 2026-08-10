#!/bin/bash
set -e
python3 - <<'PY'
import json, re
from collections import Counter

line_re = re.compile(r'^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+) [^"]*" (\d+) (\d+)')

total = 0
ips = set()
status_counts = Counter()
path_counts = Counter()
errors = 0

with open("/data/access.log") as f:
    for line in f:
        m = line_re.match(line.strip())
        if not m:
            continue
        total += 1
        ip, method, path, status, size = m.groups()
        ips.add(ip)
        status_counts[status] += 1
        path_counts[path] += 1
        if int(status) >= 400:
            errors += 1

report = {
    "total_requests": total,
    "unique_ips": len(ips),
    "status_counts": dict(status_counts),
    "top_path": path_counts.most_common(1)[0][0] if path_counts else "",
    "error_rate": round(errors / total, 3) if total else 0.0,
}
with open("/data/report.json", "w") as f:
    json.dump(report, f)
print("wrote report.json")
PY
