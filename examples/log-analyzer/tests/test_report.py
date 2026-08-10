import json
import re
from collections import Counter
from pathlib import Path


def _expected():
    line_re = re.compile(r'^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+) [^"]*" (\d+) (\d+)')
    total, errors = 0, 0
    ips, status_counts, path_counts = set(), Counter(), Counter()
    for line in Path("/data/access.log").read_text().splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        total += 1
        ip, method, path, status, size = m.groups()
        ips.add(ip); status_counts[status] += 1; path_counts[path] += 1
        if int(status) >= 400:
            errors += 1
    return {
        "total_requests": total,
        "unique_ips": len(ips),
        "status_counts": dict(status_counts),
        "top_path": path_counts.most_common(1)[0][0],
        "error_rate": round(errors / total, 3),
    }


def _load():
    return json.loads(Path("/data/report.json").read_text())


def test_report_exists():
    assert Path("/data/report.json").exists(), "report.json was not created"


def test_keys_exact():
    got = set(_load().keys())
    want = {"total_requests", "unique_ips", "status_counts", "top_path", "error_rate"}
    assert got == want, f"keys mismatch: {got} != {want}"


def test_total_requests():
    assert _load()["total_requests"] == _expected()["total_requests"]


def test_unique_ips():
    assert _load()["unique_ips"] == _expected()["unique_ips"]


def test_status_counts():
    assert _load()["status_counts"] == _expected()["status_counts"]


def test_top_path():
    assert _load()["top_path"] == _expected()["top_path"]


def test_error_rate():
    assert abs(_load()["error_rate"] - _expected()["error_rate"]) < 1e-6
