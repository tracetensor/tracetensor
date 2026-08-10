#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
from configparser import ConfigParser
from pathlib import Path


def coerce(key, value):
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    if value.isdigit() and key in {"port", "retries", "ttl"}:
        return int(value)
    return value


parser = ConfigParser()
parser.read("/data/app.ini")
out = {}
for section in parser.sections():
    out[section] = {key: coerce(key, parser.get(section, key)) for key in parser[section]}
Path("/data/config.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
PY
