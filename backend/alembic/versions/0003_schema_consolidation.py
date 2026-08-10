"""schema consolidation — absorb the startup DDL shim

Until this revision the schema had two owners: Alembic, and an
`_ensure_additive_columns()` shim that ran raw DDL on every startup. Neither was
authoritative, so a contributor writing a proper migration would collide with
the shim in production. This revision ports everything the shim did into
migration history and the shim is deleted.

What the shim did that revisions 0001/0002 did not:
  - jobs.dataset_run_id            (added with dataset support)
  - jobs/dataset_runs.idempotency_key + their UNIQUE indexes
  - created_at indexes on tasks/jobs/datasets/dataset_runs (list ORDER BY)
  - UNIQUE (name, version) on datasets
  - the _rate_limit_hits table + its composite index

A database created fresh from the current models (revision 0001 applies
Base.metadata) already has all of it, so every step here checks first and is a
no-op on a new install. The steps only fire on a database that predates the
model change in question.

Revision ID: 0003_schema_consolidation
Revises: 0002_job_queue_columns
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0003_schema_consolidation"
down_revision = "0002_job_queue_columns"
branch_labels = None
depends_on = None


def _tables(insp):
    return set(insp.get_table_names())


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(insp, table):
    names = {ix["name"] for ix in insp.get_indexes(table)}
    # A UNIQUE constraint and a UNIQUE index are the same thing to us here, but
    # some dialects report them separately — check both so we never double-create.
    try:
        names |= {uc["name"] for uc in insp.get_unique_constraints(table)}
    except NotImplementedError:  # pragma: no cover - dialect-dependent
        pass
    return names


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = _tables(insp)

    # --- Columns the shim added ------------------------------------------------
    if "jobs" in tables and "dataset_run_id" not in _columns(insp, "jobs"):
        op.add_column(
            "jobs",
            # Same type the model declares: postgresql.UUID, which SQLAlchemy
            # renders as CHAR(32) on SQLite.
            sa.Column("dataset_run_id", UUID(as_uuid=True), nullable=True),
        )
        op.create_index("ix_jobs_dataset_run_id", "jobs", ["dataset_run_id"])

    for table in ("jobs", "dataset_runs"):
        if table not in tables:
            continue
        if "idempotency_key" not in _columns(insp, table):
            op.add_column(table, sa.Column("idempotency_key", sa.String(80), nullable=True))

    # --- Indexes the shim added ------------------------------------------------
    # Refresh the inspector: the ALTERs above changed the picture.
    insp = sa.inspect(bind)

    # Unique idempotency keys — the DB-level half of the retry-safety guarantee.
    # The app pre-checks, but only this index makes a concurrent double-POST safe.
    for table, index_name in (
        ("jobs", "uq_jobs_idempotency_key"),
        ("dataset_runs", "uq_dataset_runs_idempotency_key"),
    ):
        if table in tables and index_name not in _indexes(insp, table):
            op.create_index(index_name, table, ["idempotency_key"], unique=True)

    # created_at indexes back the ORDER BY on every list endpoint.
    for table in ("tasks", "jobs", "datasets", "dataset_runs"):
        if table in tables and f"ix_{table}_created_at" not in _indexes(insp, table):
            op.create_index(f"ix_{table}_created_at", table, ["created_at"])

    # (name, version) is immutable and unique for a dataset. Enforced here rather
    # than only in app code so concurrent uploads can't create duplicates.
    if "datasets" in tables and "uq_datasets_name_version" not in _indexes(insp, "datasets"):
        op.create_index("uq_datasets_name_version", "datasets", ["name", "version"], unique=True)

    # --- The rate-limit ledger -------------------------------------------------
    # Previously created by raw DDL on Postgres only; now a real model
    # (app.models.rate_limit) so both dialects get an identical schema.
    if "_rate_limit_hits" not in tables:
        op.create_table(
            "_rate_limit_hits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("hit_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_rate_limit_hits_key_hit_at", "_rate_limit_hits", ["key", "hit_at"])


def downgrade() -> None:
    # Only the rate-limit table is safe to reverse: the columns and indexes above
    # are load-bearing for the current models, so dropping them would leave the
    # app broken at a revision it claims to support. Downgrading past 0003 means
    # downgrading the code too, and 0002's downgrade handles that.
    bind = op.get_bind()
    if "_rate_limit_hits" in _tables(sa.inspect(bind)):
        op.drop_index("ix_rate_limit_hits_key_hit_at", table_name="_rate_limit_hits")
        op.drop_table("_rate_limit_hits")
