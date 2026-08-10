"""job queue: per-job concurrency + lease columns

Adds the durable-queue columns to jobs and dataset_runs:
  jobs:          concurrency, worker_id, claimed_at, heartbeat_at
  dataset_runs:  worker_id, claimed_at, heartbeat_at

Idempotent by design: the baseline (0001) creates tables from the CURRENT models
via create_all, so on a brand-new DB these columns already exist — this revision
only ALTERs a DB that predates them. It checks before adding, mirroring the
startup shim in app/core/database.py, so it's a no-op when they're present.

Revision ID: 0002_job_queue_columns
Revises: 0001_baseline
Create Date: 2026-07-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_job_queue_columns"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_COLUMNS = {
    "jobs": [
        ("concurrency", sa.Integer(), "4"),
        ("worker_id", sa.String(120), None),
        ("claimed_at", None, None),  # timestamp — type resolved per dialect
        ("heartbeat_at", None, None),
    ],
    "dataset_runs": [
        ("worker_id", sa.String(120), None),
        ("claimed_at", None, None),
        ("heartbeat_at", None, None),
    ],
}


def _existing(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    ts_type = sa.TIMESTAMP(timezone=True)
    for table, cols in _COLUMNS.items():
        if table not in tables:
            continue
        have = _existing(insp, table)
        for name, coltype, default in cols:
            if name in have:
                continue
            col_type = coltype if coltype is not None else ts_type
            kwargs = {}
            if default is not None:
                kwargs["server_default"] = sa.text(default)
            op.add_column(table, sa.Column(name, col_type, nullable=True, **kwargs))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for table, cols in _COLUMNS.items():
        if table not in tables:
            continue
        have = _existing(insp, table)
        for name, _t, _d in cols:
            if name in have:
                op.drop_column(table, name)
