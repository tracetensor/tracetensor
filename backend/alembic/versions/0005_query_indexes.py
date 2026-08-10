"""composite indexes for the queries that actually run

Single-column indexes existed on `status` and `created_at` separately, which
answers half of "oldest queued job" and leaves the database sorting the rest.
These cover the four hot queries end to end: the queue claim, the lease reclaim,
the per-task job list, and the leaderboard.

Purely additive — no column or constraint changes, so it's safe to apply to a
live database.

Revision ID: 0005_query_indexes
Revises: 0004_job_events
Create Date: 2026-08-10
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_query_indexes"
down_revision = "0004_job_events"
branch_labels = None
depends_on = None

_INDEXES = [
    ("ix_jobs_status_created_at", "jobs", ["status", "created_at"]),
    ("ix_jobs_status_heartbeat_at", "jobs", ["status", "heartbeat_at"]),
    ("ix_jobs_task_id_created_at", "jobs", ["task_id", "created_at"]),
    (
        "ix_dataset_runs_dataset_status_created",
        "dataset_runs",
        ["dataset_id", "status", "created_at"],
    ),
    ("ix_dataset_runs_status_created_at", "dataset_runs", ["status", "created_at"]),
]


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for name, table, columns in _INDEXES:
        if table not in tables:
            continue
        if name in {ix["name"] for ix in insp.get_indexes(table)}:
            continue
        op.create_index(name, table, columns)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for name, table, _columns in _INDEXES:
        if table in tables and name in {ix["name"] for ix in insp.get_indexes(table)}:
            op.drop_index(name, table_name=table)
