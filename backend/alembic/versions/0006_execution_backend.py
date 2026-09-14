"""Add execution backend column to jobs and dataset_runs.

Revision ID: 0006_execution_backend
Revises: 0005_query_indexes
Create Date: 2026-08-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_execution_backend"
down_revision = "0005_query_indexes"
branch_labels = None
depends_on = None

_COLUMNS = {
    "jobs": ("backend", sa.String(40), "docker"),
    "dataset_runs": ("backend", sa.String(40), "docker"),
}


def _existing(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for table, (name, col_type, default) in _COLUMNS.items():
        if table not in tables:
            continue
        if name in _existing(insp, table):
            continue
        op.add_column(
            table,
            sa.Column(name, col_type, nullable=False, server_default=default),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for table, (name, _, _) in _COLUMNS.items():
        if table in tables and name in _existing(insp, table):
            op.drop_column(table, name)
