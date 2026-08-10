"""
Dataset reporting — the leaderboard and run rollups.

Read-side counterpart to job_service. It lived in datasets.py as an 88-line
route handler that mixed three concerns: fetching, aggregating, and shaping the
response. Splitting it out is what let the N+1 below get fixed — the loop was
hard to see inside a handler that was mostly response construction.

Returns plain dataclasses. The router maps them onto Pydantic response models,
so nothing here knows what the API looks like.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.dataset import Dataset, DatasetRun
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.task import Task


@dataclass
class LeaderboardColumn:
    task_id: uuid.UUID
    name: str


@dataclass
class LeaderboardCellData:
    task_id: uuid.UUID
    pass_rate: Optional[float]
    job_id: Optional[uuid.UUID]
    status: Optional[str]


@dataclass
class LeaderboardRowData:
    agent: str
    model: Optional[str]
    run_id: uuid.UUID
    run_count: int
    overall_pass_rate: Optional[float]
    created_at: datetime
    cells: list = field(default_factory=list)


@dataclass
class LeaderboardData:
    dataset: Dataset
    columns: list
    rows: list


class DatasetVersionConflict(Exception):
    """A (name, version) already exists with different content. Versions are
    immutable — a published dataset can't change under a benchmark that cites it."""


async def register_bundle(
    db: AsyncSession, zip_bytes: bytes, tasks_root: Path
) -> tuple[Dataset, bool]:
    """Register a dataset ZIP: store every task, then record the manifest.

    Returns (dataset, created). `created=False` means an identical bundle was
    already registered — re-uploading the same bytes is a no-op, which is what
    makes an interrupted upload safe to retry.

    Raises DatasetParseError for a malformed bundle and DatasetVersionConflict
    when the version exists with different content.
    """
    from pathlib import Path

    from app.services.dataset_parser import compute_content_hash, extract_bundle
    from app.services.task_parser import TaskParseError, parse_task_toml
    from app.services.task_service import default_task_toml, persist_task
    from app.storage.task_store import store_task_from_files

    bundle = extract_bundle(zip_bytes)
    manifest = bundle.manifest
    content_hash = compute_content_hash(bundle.task_files)

    existing = (
        await db.execute(
            select(Dataset).where(
                Dataset.name == manifest.name, Dataset.version == manifest.version
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash == content_hash:
            return existing, False
        raise DatasetVersionConflict(
            f"Dataset '{manifest.name}' version '{manifest.version}' already exists "
            "with different content. Bump the version to publish a change."
        )

    # Manifest order is preserved — it's the leaderboard's column order.
    task_ids: list = []
    task_names: list = []
    for folder in manifest.tasks:
        files = dict(bundle.task_files[folder])
        if "task.toml" not in files:
            files["task.toml"] = default_task_toml(folder)
        try:
            tname = parse_task_toml(files["task.toml"], Path(folder)).name
        except TaskParseError:
            # A task whose toml won't parse still gets stored under its folder
            # name; validate_task records the failure on the row, so the dataset
            # registers with that task marked not-ready rather than the whole
            # upload failing.
            tname = folder
        task_dir = store_task_from_files(tasks_root, tname, files)
        task, _ = await persist_task(db, task_dir)
        task_ids.append(str(task.id))
        task_names.append(task.name)

    ds = Dataset(
        id=uuid.uuid4(),
        name=manifest.name,
        version=manifest.version,
        content_hash=content_hash,
        task_ids=task_ids,
        task_names=task_names,
        description=manifest.description or None,
    )
    db.add(ds)
    await db.commit()
    await db.refresh(ds)
    return ds, True


async def build_leaderboard(db: AsyncSession, dataset_id: uuid.UUID) -> Optional[LeaderboardData]:
    """One row per (agent, model), one column per task; None if no such dataset.

    Each cell is that combo's *latest completed* run on that task, carrying the
    job id so the UI can drill into the trajectory. Rows sort by overall pass
    rate, descending.
    """
    ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
    if ds is None:
        return None

    # Column order comes from the manifest, not from the query — the manifest is
    # the dataset's declared task order and the leaderboard must preserve it.
    ids = [uuid.UUID(t) for t in (ds.task_ids or [])]
    name_by_id = {}
    if ids:
        rows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
        name_by_id = {t.id: t.name for t in rows}
    stored_names = ds.task_names or []
    columns = [
        LeaderboardColumn(
            task_id=tid,
            name=name_by_id.get(tid) or (stored_names[i] if i < len(stored_names) else str(tid)),
        )
        for i, tid in enumerate(ids)
    ]

    # One row per (agent, model): the newest completed run, plus how many there
    # have been. Done in SQL rather than by loading every completed run and
    # grouping in Python — a dataset with 10,000 runs used to pull all 10,000
    # rows across the wire to render a page that shows a handful.
    #
    # ROW_NUMBER() rather than DISTINCT ON so this works on SQLite too, which the
    # dev database and most of the test suite use.
    ranked = (
        select(
            DatasetRun,
            func.row_number()
            .over(
                partition_by=(DatasetRun.agent, func.coalesce(DatasetRun.model, "")),
                order_by=DatasetRun.created_at.desc(),
            )
            .label("rank"),
            func.count()
            .over(partition_by=(DatasetRun.agent, func.coalesce(DatasetRun.model, "")))
            .label("run_count"),
        )
        .where(
            DatasetRun.dataset_id == dataset_id,
            DatasetRun.status == JobStatus.COMPLETED,
        )
        .subquery()
    )
    aliased_run = aliased(DatasetRun, ranked)
    latest_rows = (
        await db.execute(select(aliased_run, ranked.c.run_count).where(ranked.c.rank == 1))
    ).all()

    groups: dict = {}  # (agent, model) -> {"latest": run, "count": int}
    for run, run_count in latest_rows:
        groups[(run.agent, run.model or "")] = {"latest": run, "count": run_count}

    # One query for every latest run's jobs, not one query per row. With 10
    # agents x 10 models that was 100 round-trips to render a single page.
    latest_ids = [g["latest"].id for g in groups.values()]
    jobs_by_run: dict = {}
    if latest_ids:
        jobs = (
            (await db.execute(select(Job).where(Job.dataset_run_id.in_(latest_ids))))
            .scalars()
            .all()
        )
        for j in jobs:
            jobs_by_run.setdefault(j.dataset_run_id, {})[j.task_id] = j

    out_rows: list = []
    for (agent, model), g in groups.items():
        run = g["latest"]
        job_by_task = jobs_by_run.get(run.id, {})
        cells, rates = [], []
        for tid in ids:
            j = job_by_task.get(tid)
            cells.append(
                LeaderboardCellData(
                    task_id=tid,
                    pass_rate=j.pass_rate if j else None,
                    job_id=j.id if j else None,
                    status=j.status if j else None,
                )
            )
            if j is not None and j.pass_rate is not None:
                rates.append(j.pass_rate)
        out_rows.append(
            LeaderboardRowData(
                agent=agent,
                model=(model or None),
                run_id=run.id,
                run_count=g["count"],
                overall_pass_rate=(sum(rates) / len(rates) if rates else None),
                created_at=run.created_at,
                cells=cells,
            )
        )

    # A row with no scored task sorts last rather than crashing the comparison.
    out_rows.sort(
        key=lambda r: r.overall_pass_rate if r.overall_pass_rate is not None else -1.0,
        reverse=True,
    )
    return LeaderboardData(dataset=ds, columns=columns, rows=out_rows)


async def list_runs_with_rates(
    db: AsyncSession, dataset_id: uuid.UUID, limit: int, offset: int
) -> tuple[Sequence[DatasetRun], dict, int]:
    """A page of runs, their pass rates, and the total count for pagination.

    Rates come from one query across every run on the page — this was a query
    per run before.
    """
    from sqlalchemy import func

    total = (
        await db.execute(
            select(func.count()).select_from(DatasetRun).where(DatasetRun.dataset_id == dataset_id)
        )
    ).scalar_one()

    runs = (
        (
            await db.execute(
                select(DatasetRun)
                .where(DatasetRun.dataset_id == dataset_id)
                .order_by(DatasetRun.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    rates_by_run: dict = {}
    run_ids = [r.id for r in runs]
    if run_ids:
        jobs = (
            (await db.execute(select(Job).where(Job.dataset_run_id.in_(run_ids)))).scalars().all()
        )
        for j in jobs:
            if j.pass_rate is not None:
                rates_by_run.setdefault(j.dataset_run_id, []).append(j.pass_rate)

    return runs, rates_by_run, total


__all__ = [
    "DatasetVersionConflict",
    "LeaderboardCellData",
    "LeaderboardColumn",
    "LeaderboardData",
    "LeaderboardRowData",
    "build_leaderboard",
    "register_bundle",
    "list_runs_with_rates",
]
