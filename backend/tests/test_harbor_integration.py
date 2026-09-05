"""Optional live Harbor Hub integration (skipped unless TRACETENSOR_HUB_INTEGRATION=1)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("TRACETENSOR_HUB_INTEGRATION") != "1",
    reason="Set TRACETENSOR_HUB_INTEGRATION=1 for live Hub tests",
)


def test_live_pull_orca_bench(tmp_path):
    from app.services.harbor_registry import HarborRegistryClient
    from app.services.task_validator import STATUS_READY, validate_task

    client = HarborRegistryClient()
    result = client.download_task(
        "orca-bench/032b3bef243e177f",
        output_dir=tmp_path,
        overwrite=True,
    )
    res = validate_task(result.path)
    assert res.status == STATUS_READY
