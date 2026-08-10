"""live-progress event log

Adds `job_events`, the durable backing for the SSE streams. Progress used to
live only in a per-process dict, so a worker on another machine wrote its events
into its own memory and the web tier serving the browser never saw them — live
progress silently didn't work in exactly the multi-machine setup the docs
recommend. See app/services/event_bus.py.

Revision ID: 0004_job_events
Revises: 0003_schema_consolidation
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_job_events"
down_revision = "0003_schema_consolidation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "job_events" in set(sa.inspect(bind).get_table_names()):
        return  # a database built from the models already has it

    op.create_table(
        "job_events",
        # The id doubles as the client's stream cursor ("everything after N"),
        # which is why it has to be a real sequence and not a timestamp.
        # INTEGER on SQLite, not BIGINT: only `INTEGER PRIMARY KEY` is SQLite's
        # auto-incrementing rowid alias. See app/models/event.py.
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("payload", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Every read is "this channel, after this id" — both columns, so the query
    # never has to look at another channel's rows.
    op.create_index("ix_job_events_channel_id", "job_events", ["channel", "id"])
    # Pruning is by age; without this it degrades into a full scan as the log grows.
    op.create_index("ix_job_events_created_at", "job_events", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if "job_events" not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_index("ix_job_events_created_at", table_name="job_events")
    op.drop_index("ix_job_events_channel_id", table_name="job_events")
    op.drop_table("job_events")
