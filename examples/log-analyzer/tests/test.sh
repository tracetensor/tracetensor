#!/bin/bash
# Health check for the log-analyzer task.
# Prefers pytest; if unavailable, falls back to a stdlib test runner so scoring
# still works anywhere. Writes partial-credit reward.json.

mkdir -p /logs/verifier

if python3 -c "import pytest" 2>/dev/null; then
  python3 -m pytest /tests/test_report.py -q --tb=no > /tmp/pytest.out 2>&1
else
  python3 - <<'PY' > /tmp/pytest.out 2>&1
import importlib.util
spec = importlib.util.spec_from_file_location("t", "/tests/test_report.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tests = [f for n, f in vars(m).items() if n.startswith("test_") and callable(f)]
p = f = 0
for t in tests:
    try:
        t(); p += 1
    except Exception as e:
        f += 1; print(f"FAIL {t.__name__}: {e}")
print(f"{p} passed, {f} failed")
PY
fi

cat /tmp/pytest.out
passed=$(grep -oE '[0-9]+ passed' /tmp/pytest.out | grep -oE '[0-9]+' | head -1)
failed=$(grep -oE '[0-9]+ failed' /tmp/pytest.out | grep -oE '[0-9]+' | head -1)
passed=${passed:-0}; failed=${failed:-0}
total=$((passed + failed))

if [ "$total" -eq 0 ]; then
  echo '{"report_correct": 0.0}' > /logs/verifier/reward.json
else
  score=$(python3 -c "print(round($passed/$total, 3))")
  echo "{\"tests_passed\": $passed, \"tests_total\": $total, \"report_correct\": $score}" > /logs/verifier/reward.json
fi
cat /logs/verifier/reward.json
