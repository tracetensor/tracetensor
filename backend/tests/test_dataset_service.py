"""
dataset_service — the leaderboard and bundle registration.

The leaderboard is the product's headline output: it's what someone points at to
claim one agent beats another. It had no test at all while it lived inside an
88-line route handler, which is also why the N+1 query in it went unnoticed.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from app.models.dataset import Dataset, DatasetRun
from app.models.enums import JobStatus
from app.models.job import Job
from app.services import dataset_service


async def make_dataset(db, task_ids, name="suite", version="1.0"):
    ds = Dataset(
        id=uuid.uuid4(),
        name=name,
        version=version,
        content_hash=f"h-{name}-{version}",
        task_ids=[str(t) for t in task_ids],
        task_names=[f"task-{i}" for i, _ in enumerate(task_ids)],
    )
    db.add(ds)
    await db.commit()
    return ds


async def make_run(db, ds, agent, model, cells, *, age_minutes=0, status=JobStatus.COMPLETED):
    """A completed run plus one child job per (task_id, pass_rate) in `cells`."""
    run = DatasetRun(
        id=uuid.uuid4(),
        dataset_id=ds.id,
        agent=agent,
        model=model,
        n_trials=1,
        concurrency=1,
        status=status.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    db.add(run)
    await db.flush()
    for task_id, rate in cells.items():
        db.add(
            Job(
                id=uuid.uuid4(),
                task_id=task_id,
                dataset_run_id=run.id,
                agent=agent,
                model=model,
                n_trials=1,
                concurrency=1,
                status=JobStatus.COMPLETED.value,
                pass_rate=rate,
            )
        )
    await db.commit()
    return run


async def leaderboard_of(db, dataset_id):
    data = await dataset_service.build_leaderboard(db, dataset_id)
    assert data is not None, f"no leaderboard for dataset {dataset_id}"
    return data


class TestLeaderboard:
    async def test_unknown_dataset_returns_none(self, db):
        assert await dataset_service.build_leaderboard(db, uuid.uuid4()) is None

    async def test_one_row_per_agent_model_pair(self, db, ready_task):
        ds = await make_dataset(db, [ready_task.id])
        await make_run(db, ds, "oracle", None, {ready_task.id: 1.0})
        await make_run(db, ds, "claude-code", "haiku", {ready_task.id: 0.5})

        data = await leaderboard_of(db, ds.id)
        assert {(r.agent, r.model) for r in data.rows} == {
            ("oracle", None),
            ("claude-code", "haiku"),
        }

    async def test_rows_sort_by_pass_rate_descending(self, db, ready_task):
        ds = await make_dataset(db, [ready_task.id])
        await make_run(db, ds, "weak", "m", {ready_task.id: 0.2})
        await make_run(db, ds, "strong", "m", {ready_task.id: 0.9})

        data = await leaderboard_of(db, ds.id)
        assert [r.agent for r in data.rows] == ["strong", "weak"]

    async def test_an_unscored_row_sorts_last_instead_of_crashing(self, db, ready_task):
        """A run whose jobs all have pass_rate=None can't be compared to a float.
        It must sort to the bottom, not raise."""
        ds = await make_dataset(db, [ready_task.id])
        await make_run(db, ds, "scored", "m", {ready_task.id: 0.4})
        await make_run(db, ds, "unscored", "m", {ready_task.id: None})

        data = await leaderboard_of(db, ds.id)
        assert [r.agent for r in data.rows] == ["scored", "unscored"]
        assert data.rows[-1].overall_pass_rate is None

    async def test_only_the_latest_run_per_pair_is_shown(self, db, ready_task):
        ds = await make_dataset(db, [ready_task.id])
        await make_run(db, ds, "oracle", "m", {ready_task.id: 0.1}, age_minutes=60)
        latest = await make_run(db, ds, "oracle", "m", {ready_task.id: 0.9}, age_minutes=0)

        data = await leaderboard_of(db, ds.id)
        row = data.rows[0]
        assert row.run_id == latest.id
        assert row.overall_pass_rate == 0.9
        assert row.run_count == 2  # but both are counted

    async def test_unfinished_runs_are_excluded(self, db, ready_task):
        """A running row would show a partial score as if it were a result."""
        ds = await make_dataset(db, [ready_task.id])
        await make_run(db, ds, "oracle", "m", {ready_task.id: 0.5}, status=JobStatus.RUNNING)
        data = await leaderboard_of(db, ds.id)
        assert data.rows == []

    async def test_columns_follow_manifest_order_not_query_order(self, db, ready_task, tmp_path):
        """Column order is the dataset's declared task order. Letting the DB
        decide would reshuffle the board between renders."""
        from app.services.task_service import persist_task

        second_dir = tmp_path / "second"
        (second_dir / "tests").mkdir(parents=True)
        (second_dir / "instruction.md").write_text("x")
        (second_dir / "tests" / "test.sh").write_text("echo ok")
        (second_dir / "task.toml").write_text('[task]\nname = "test/second"\n')
        second, _ = await persist_task(db, second_dir)

        ds = await make_dataset(db, [second.id, ready_task.id])
        data = await leaderboard_of(db, ds.id)
        assert [c.task_id for c in data.columns] == [second.id, ready_task.id]

    async def test_a_task_the_run_never_covered_gets_an_empty_cell(self, db, ready_task):
        """Missing must read as missing, not as a zero — a task that didn't run
        is not a task the agent failed."""
        missing_id = uuid.uuid4()
        ds = await make_dataset(db, [ready_task.id, missing_id])
        await make_run(db, ds, "oracle", "m", {ready_task.id: 1.0})

        cells = (await leaderboard_of(db, ds.id)).rows[0].cells
        assert cells[0].pass_rate == 1.0
        assert cells[1].pass_rate is None and cells[1].job_id is None

    async def test_overall_rate_averages_only_scored_tasks(self, db, ready_task):
        missing_id = uuid.uuid4()
        ds = await make_dataset(db, [ready_task.id, missing_id])
        await make_run(db, ds, "oracle", "m", {ready_task.id: 1.0})
        # 1.0 over one scored task — not 0.5 over two.
        assert (await leaderboard_of(db, ds.id)).rows[0].overall_pass_rate == 1.0


class TestRunListing:
    async def test_paginates_newest_first_with_a_total(self, db, ready_task):
        ds = await make_dataset(db, [ready_task.id])
        for i in range(4):
            await make_run(db, ds, f"a{i}", "m", {ready_task.id: 0.5}, age_minutes=10 - i)

        runs, rates, total = await dataset_service.list_runs_with_rates(
            db, ds.id, limit=2, offset=0
        )
        assert total == 4
        assert len(runs) == 2
        assert runs[0].agent == "a3"  # newest
        assert rates[runs[0].id] == [0.5]

    async def test_offset_walks_the_pages(self, db, ready_task):
        ds = await make_dataset(db, [ready_task.id])
        for i in range(3):
            await make_run(db, ds, f"a{i}", "m", {ready_task.id: 1.0}, age_minutes=10 - i)

        page2, _rates, _total = await dataset_service.list_runs_with_rates(
            db, ds.id, limit=2, offset=2
        )
        assert [r.agent for r in page2] == ["a0"]


def bundle_zip(tasks: dict, manifest: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("dataset.toml", manifest)
        for folder, files in tasks.items():
            for rel, body in files.items():
                z.writestr(f"{folder}/{rel}", body)
    return buf.getvalue()


TASK_FILES = {
    "instruction.md": "do it",
    "tests/test.sh": "echo ok",
    "task.toml": '[task]\nname = "suite/alpha"\n',
}


class TestBundleRegistration:
    async def test_registers_tasks_and_records_the_manifest(self, db, tmp_path):
        data = bundle_zip(
            {"alpha": TASK_FILES},
            'name = "suite"\nversion = "1.0"\ntasks = ["alpha"]\n',
        )
        ds, created = await dataset_service.register_bundle(db, data, tmp_path)
        assert created is True
        assert ds.name == "suite" and ds.version == "1.0"
        assert len(ds.task_ids) == 1

    async def test_identical_reupload_is_a_no_op(self, db, tmp_path):
        """An interrupted upload has to be safe to retry."""
        data = bundle_zip(
            {"alpha": TASK_FILES},
            'name = "suite"\nversion = "1.0"\ntasks = ["alpha"]\n',
        )
        first, created_a = await dataset_service.register_bundle(db, data, tmp_path)
        second, created_b = await dataset_service.register_bundle(db, data, tmp_path)

        assert created_a is True and created_b is False
        assert first.id == second.id

    async def test_changed_content_under_the_same_version_is_rejected(self, db, tmp_path):
        """A published (name, version) is immutable — a benchmark that cites it
        must keep meaning the same thing."""
        manifest = 'name = "suite"\nversion = "1.0"\ntasks = ["alpha"]\n'
        await dataset_service.register_bundle(
            db, bundle_zip({"alpha": TASK_FILES}, manifest), tmp_path
        )

        changed = {**TASK_FILES, "instruction.md": "do something else"}
        with pytest.raises(dataset_service.DatasetVersionConflict, match="Bump the version"):
            await dataset_service.register_bundle(
                db, bundle_zip({"alpha": changed}, manifest), tmp_path
            )

    async def test_a_new_version_is_allowed(self, db, tmp_path):
        await dataset_service.register_bundle(
            db,
            bundle_zip(
                {"alpha": TASK_FILES}, 'name = "suite"\nversion = "1.0"\ntasks = ["alpha"]\n'
            ),
            tmp_path,
        )
        changed = {**TASK_FILES, "instruction.md": "v2 instructions"}
        ds2, created = await dataset_service.register_bundle(
            db,
            bundle_zip({"alpha": changed}, 'name = "suite"\nversion = "2.0"\ntasks = ["alpha"]\n'),
            tmp_path,
        )
        assert created is True and ds2.version == "2.0"

    async def test_a_task_folder_without_task_toml_gets_a_default(self, db, tmp_path):
        files = {k: v for k, v in TASK_FILES.items() if k != "task.toml"}
        ds, _ = await dataset_service.register_bundle(
            db,
            bundle_zip(
                {"alpha": files}, 'name = "defaulted"\nversion = "1.0"\ntasks = ["alpha"]\n'
            ),
            tmp_path,
        )
        assert ds.task_names == ["alpha"]
