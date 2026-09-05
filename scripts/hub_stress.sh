#!/usr/bin/env bash
# Harbor Hub stress tier (T2) — manual / nightly. Requires network + optional Docker.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
PY="${BACKEND}/venv/bin/python"
TT="${PY} -m app.cli.main"

if [[ "${TRACETENSOR_HUB_STRESS:-}" != "1" ]]; then
  echo "Set TRACETENSOR_HUB_STRESS=1 to run live Hub stress tests."
  exit 0
fi

echo "== T2: live task pull + validate =="
$TT tasks pull orca-bench/032b3bef243e177f -o "$ROOT/tasks-stress" --overwrite
$TT tasks validate "$ROOT/tasks-stress/032b3bef243e177f"

echo "== T2: cache hit (second pull) =="
$TT tasks pull orca-bench/032b3bef243e177f -o "$ROOT/tasks-stress"

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "== T2: oracle smoke on pulled task =="
  $TT run "$ROOT/tasks-stress/032b3bef243e177f" -a oracle -n 1 --no-save --platform linux/amd64
fi

echo "== T2 PASS =="
