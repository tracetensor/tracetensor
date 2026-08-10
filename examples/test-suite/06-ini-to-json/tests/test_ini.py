import json
from configparser import ConfigParser
from pathlib import Path


def _coerce(key, value):
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    if value.isdigit() and key in {"port", "retries", "ttl"}:
        return int(value)
    return value


def expected():
    parser = ConfigParser()
    parser.read("/data/app.ini")
    out = {}
    for section in parser.sections():
        out[section] = {
            key: _coerce(key, parser.get(section, key))
            for key in parser[section]
        }
    return out


out_path = Path("/data/config.json")
assert out_path.exists(), "config.json was not created"
got = json.loads(out_path.read_text(encoding="utf-8"))
want = expected()
assert got == want, f"expected {want}, got {got}"
print("ini ok")
