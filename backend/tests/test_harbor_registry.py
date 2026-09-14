"""Tests for Harbor Hub registry client (mocked HTTP)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.schemas.registry import parse_package_ref
from app.services.harbor_registry import HarborRegistryClient, RegistryNotFoundError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hub"


def _make_task_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'schema_version = "1.3"\n'
        info = tarfile.TarInfo(name="task.toml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_parse_package_ref() -> None:
    ref = parse_package_ref("orca-bench/032b3bef243e177f@latest")
    assert ref.org == "orca-bench"
    assert ref.name == "032b3bef243e177f"
    assert ref.ref == "latest"


def test_parse_package_ref_hub_prefix() -> None:
    ref = parse_package_ref("hub:test-org/my-task")
    assert ref.org == "test-org"
    assert ref.ref == "latest"


def test_parse_package_ref_invalid() -> None:
    with pytest.raises(ValueError):
        parse_package_ref("not-a-package")


def test_download_task_uses_cache(tmp_path: Path) -> None:
    resolved = json.loads((FIXTURES / "resolve_task_version.json").read_text())
    archive_bytes = _make_task_tar()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rpc/resolve_task_version"):
            return httpx.Response(200, json=resolved)
        if "/storage/v1/object/packages/" in request.url.path:
            return httpx.Response(200, content=archive_bytes)
        return httpx.Response(404)

    client = HarborRegistryClient()
    transport = httpx.MockTransport(handler)
    with patch.object(client, "_client") as mock_client:
        mock_client.return_value.__enter__.return_value = httpx.Client(
            transport=transport, base_url="https://example.test"
        )
        first = client.download_task(
            "orca-bench/032b3bef243e177f",
            output_dir=tmp_path,
            overwrite=False,
        )
        assert first.cached is False
        assert (first.path / "task.toml").exists()

        second = client.download_task(
            "orca-bench/032b3bef243e177f",
            output_dir=tmp_path,
            overwrite=False,
        )
        assert second.cached is True


def test_resolve_task_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=None)

    client = HarborRegistryClient()
    transport = httpx.MockTransport(handler)
    with patch.object(client, "_client") as mock_client:
        mock_client.return_value.__enter__.return_value = httpx.Client(
            transport=transport, base_url="https://example.test"
        )
        with pytest.raises(RegistryNotFoundError):
            client.resolve_task_version("missing", "task", "latest")


def test_download_dataset_bounded(tmp_path: Path) -> None:
    fixtures = json.loads((FIXTURES / "dataset_mini.json").read_text())
    resolved = json.loads((FIXTURES / "resolve_task_version.json").read_text())
    archive_bytes = _make_task_tar()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dataset_version_tag"):
            return httpx.Response(200, json=fixtures["dataset_version_tag"])
        if request.url.path.endswith("/dataset_version_task"):
            return httpx.Response(200, json=fixtures["dataset_version_task"])
        if request.url.path.endswith("/rpc/resolve_task_version"):
            return httpx.Response(200, json=resolved)
        if "/storage/v1/object/packages/" in request.url.path:
            return httpx.Response(200, content=archive_bytes)
        return httpx.Response(404)

    client = HarborRegistryClient()
    transport = httpx.MockTransport(handler)
    with patch.object(client, "_client") as mock_client:
        mock_client.return_value.__enter__.return_value = httpx.Client(
            transport=transport, base_url="https://example.test"
        )
        result = client.download_dataset("test-org/mini-set@latest", output_dir=tmp_path)
        assert len(result.paths) == 2
        assert result.dataset_dir.is_dir()
