"""Harbor Hub registry client — direct Supabase REST + storage (no Harbor CLI).

Mirrors Harbor's public registry flow:
  resolve_task_version RPC → download packages bucket archive → extract .tar.gz

Defaults match harbor-framework/harbor auth constants (public publishable key).
"""

from __future__ import annotations

import os
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.schemas.registry import PackageRef, RefType, parse_package_ref
from app.services.registry_cache import (
    dataset_export_root,
    default_cache_root,
    ensure_empty_dir,
    is_cached,
    task_cache_dir,
    task_export_dir,
)

ARCHIVE_NAME = "task.tar.gz"
PACKAGES_BUCKET = "packages"

DEFAULT_SUPABASE_URL = "https://ofhuhcpkvzjlejydnvyd.supabase.co"
DEFAULT_PUBLISHABLE_KEY = "sb_publishable_Z-vuQbpvpG-PStjbh4yE0Q_e-d3MTIH"

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_WORKERS = 4


class RegistryError(Exception):
    """Base registry error."""


class RegistryNotFoundError(RegistryError):
    pass


class RegistryAuthError(RegistryError):
    pass


@dataclass(frozen=True)
class ResolvedTaskVersion:
    id: str
    archive_path: str
    content_hash: str
    yanked_at: str | None = None
    yanked_reason: str | None = None


@dataclass(frozen=True)
class TaskDownloadResult:
    path: Path
    cached: bool
    content_hash: str


@dataclass(frozen=True)
class DatasetDownloadResult:
    paths: list[Path]
    dataset_dir: Path


def _supabase_url() -> str:
    return (os.getenv("TRACETENSOR_REGISTRY_URL") or os.getenv("HARBOR_SUPABASE_URL") or DEFAULT_SUPABASE_URL).rstrip("/")


def _publishable_key() -> str:
    return (
        os.getenv("TRACETENSOR_REGISTRY_TOKEN")
        or os.getenv("HARBOR_SUPABASE_PUBLISHABLE_KEY")
        or DEFAULT_PUBLISHABLE_KEY
    )


def _headers() -> dict[str, str]:
    key = _publishable_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


class HarborRegistryClient:
    """Sync Harbor Hub client using Supabase REST + storage."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._base = _supabase_url()

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self._base, timeout=self.timeout, headers=_headers())

    def resolve_task_version(self, org: str, name: str, ref: str = "latest") -> ResolvedTaskVersion:
        payload = {"p_org": org, "p_name": name, "p_ref": ref or "latest"}
        with self._client() as client:
            resp = client.post("/rest/v1/rpc/resolve_task_version", json=payload)
        if resp.status_code == 401:
            raise RegistryAuthError("Registry authentication failed (private package?).")
        if resp.status_code == 404 or resp.status_code == 406 or not resp.content:
            raise RegistryNotFoundError(f"Task not found: {org}/{name}@{ref}")
        if resp.status_code >= 400:
            raise RegistryError(f"resolve_task_version failed ({resp.status_code}): {resp.text[:500]}")
        row = resp.json()
        if not row:
            raise RegistryNotFoundError(f"Task not found: {org}/{name}@{ref}")
        return ResolvedTaskVersion(
            id=row["id"],
            archive_path=row["archive_path"],
            content_hash=row["content_hash"],
            yanked_at=row.get("yanked_at"),
            yanked_reason=row.get("yanked_reason"),
        )

    def _download_storage_object(self, storage_path: str, dest: Path) -> None:
        encoded = quote(storage_path, safe="/")
        url = f"/storage/v1/object/{PACKAGES_BUCKET}/{encoded}"
        with self._client() as client:
            resp = client.get(url)
        if resp.status_code == 401:
            raise RegistryAuthError("Storage download requires authentication.")
        if resp.status_code == 404:
            raise RegistryNotFoundError(f"Archive not found: {storage_path}")
        if resp.status_code >= 400:
            raise RegistryError(f"Storage download failed ({resp.status_code}): {resp.text[:300]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)

    @staticmethod
    def _extract_archive(archive: Path, target_dir: Path) -> None:
        ensure_empty_dir(target_dir, overwrite=True)
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extractall(path=target_dir, filter="data")
            except TypeError:
                # Python < 3.12 has no filter= kwarg
                tar.extractall(path=target_dir)

    def download_task(
        self,
        source: str,
        *,
        output_dir: Path | None = None,
        export: bool = True,
        overwrite: bool = False,
    ) -> TaskDownloadResult:
        ref = parse_package_ref(source)
        resolved = self.resolve_task_version(ref.org, ref.name, ref.ref)

        if export:
            base = output_dir or Path("tasks")
            target = task_export_dir(base, ref.name)
        else:
            target = task_cache_dir(ref.org, ref.name, resolved.content_hash)

        if is_cached(target) and not overwrite:
            return TaskDownloadResult(path=target, cached=True, content_hash=resolved.content_hash)

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / ARCHIVE_NAME
            self._download_storage_object(resolved.archive_path, archive)
            self._extract_archive(archive, target)

        return TaskDownloadResult(
            path=target,
            cached=False,
            content_hash=resolved.content_hash,
        )

    def _resolve_dataset_version_id(self, ref: PackageRef) -> str:
        """Return dataset_version.id for org/name@ref via PostgREST."""
        with self._client() as client:
            if ref.ref_type == RefType.TAG:
                resp = client.get(
                    "/rest/v1/dataset_version_tag",
                    params={
                        "select": "dataset_version_id,tag,package:package_id(name,type,org:org_id(name))",
                        "tag": f"eq.{ref.ref}",
                    },
                )
                if resp.status_code >= 400:
                    raise RegistryError(f"Dataset lookup failed ({resp.status_code}): {resp.text[:300]}")
                rows = [
                    r
                    for r in (resp.json() or [])
                    if (r.get("package") or {}).get("name") == ref.name
                    and ((r.get("package") or {}).get("org") or {}).get("name") == ref.org
                    and (r.get("package") or {}).get("type") == "dataset"
                ]
                if not rows:
                    raise RegistryNotFoundError(f"Dataset not found: {ref.qualified}")
                return rows[0]["dataset_version_id"]

            if ref.ref_type == RefType.REVISION:
                resp = client.get(
                    "/rest/v1/dataset_version",
                    params={
                        "select": "id,revision,package:package_id(name,type,org:org_id(name))",
                        "revision": f"eq.{ref.ref}",
                    },
                )
            else:
                digest = ref.ref.removeprefix("sha256:")
                resp = client.get(
                    "/rest/v1/dataset_version",
                    params={
                        "select": "id,content_hash,package:package_id(name,type,org:org_id(name))",
                        "content_hash": f"eq.{digest}",
                    },
                )

        if resp.status_code >= 400:
            raise RegistryError(f"Dataset lookup failed ({resp.status_code}): {resp.text[:300]}")
        rows = [
            r
            for r in (resp.json() or [])
            if (r.get("package") or {}).get("name") == ref.name
            and ((r.get("package") or {}).get("org") or {}).get("name") == ref.org
            and (r.get("package") or {}).get("type") == "dataset"
        ]
        if not rows:
            raise RegistryNotFoundError(f"Dataset not found: {ref.qualified}")
        return rows[0]["id"]

    def _list_dataset_tasks(self, dataset_version_id: str) -> list[tuple[str, str, str]]:
        """Return (org, task_name, content_hash) for each task in a dataset version."""
        params = {
            "select": "task_version:task_version_id(content_hash,package:package_id(name,org:org_id(name)))",
            "dataset_version_id": f"eq.{dataset_version_id}",
        }
        with self._client() as client:
            resp = client.get("/rest/v1/dataset_version_task", params=params)
        if resp.status_code >= 400:
            raise RegistryError(f"Dataset tasks lookup failed ({resp.status_code}): {resp.text[:300]}")
        out: list[tuple[str, str, str]] = []
        for row in resp.json() or []:
            tv = row.get("task_version") or {}
            pkg = tv.get("package") or {}
            org_block = pkg.get("org") or {}
            org = org_block.get("name")
            name = pkg.get("name")
            content_hash = tv.get("content_hash")
            if org and name and content_hash:
                out.append((org, name, content_hash))
        return out

    def _download_task_by_hash(
        self,
        org: str,
        name: str,
        content_hash: str,
        target: Path,
        *,
        overwrite: bool,
    ) -> Path:
        resolved = self.resolve_task_version(org, name, f"sha256:{content_hash}")
        if is_cached(target) and not overwrite:
            return target
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / ARCHIVE_NAME
            self._download_storage_object(resolved.archive_path, archive)
            self._extract_archive(archive, target)
        return target

    def download_dataset(
        self,
        source: str,
        *,
        output_dir: Path | None = None,
        overwrite: bool = False,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> DatasetDownloadResult:
        ref = parse_package_ref(source)
        version_id = self._resolve_dataset_version_id(ref)
        tasks = self._list_dataset_tasks(version_id)
        if not tasks:
            raise RegistryNotFoundError(f"Dataset {ref.qualified} has no tasks.")

        base = output_dir or Path("tasks")
        dataset_dir = dataset_export_root(base, ref.name)
        dataset_dir.mkdir(parents=True, exist_ok=True)

        paths: list[Path] = []

        def _one(item: tuple[str, str, str]) -> Path:
            org, name, content_hash = item
            target = dataset_dir / name
            return self._download_task_by_hash(org, name, content_hash, target, overwrite=overwrite)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_one, t): t for t in tasks}
            for fut in as_completed(futures):
                paths.append(fut.result())

        return DatasetDownloadResult(paths=sorted(paths, key=lambda p: p.name), dataset_dir=dataset_dir)


def get_registry_client() -> HarborRegistryClient:
    return HarborRegistryClient()
