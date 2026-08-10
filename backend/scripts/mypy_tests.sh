#!/usr/bin/env bash
# Type-check the pytest suites.
#
# Separate from `mypy app` because the two want different strictness: app/ has
# disallow_untyped_defs on, tests don't (annotating every test function is noise).
# The pre-pytest script suites and the pydantic shim are excluded — they're
# dynamic by construction, so checking them describes the design, not a defect.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" -m mypy --allow-untyped-defs \
  tests/conftest.py \
  tests/test_api_contract.py \
  tests/test_dataset_service.py \
  tests/test_frontend_contract.py \
  tests/test_frontend_smoke.py \
  tests/test_job_service.py \
  tests/test_legacy_suites.py \
  tests/test_llm_agent_unit.py \
  tests/test_routers_unit.py \
  tests/test_trial_runner_unit.py
