"""daily_load table (LOAD-10, first half)

One engine-computed training-load row per (athlete_id, date, sport), plus a
``combined`` sport row per day (brief §6: engine outputs persisted with
``engine_version``). The unique constraint
``uq_daily_load_athlete_date_sport`` is the idempotency anchor for the
recompute upserts: re-running the persistence for the same window updates
the existing rows instead of duplicating them.

``methods`` (JSONB) persists which load method (power/pace_speed/hr/srpe;
§7.1) produced the load, per day and sport, as a method->count mapping.

Revision ID: e5f6a7b8c9d0
Revises: b7e4c9a1d2f3
Create Date: 2026-10-03 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "b7e4c9a1d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_load",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("sport", sa.String(length=32), nullable=False),
        sa.Column("tss", sa.Float(), nullable=False),
        sa.Column("ctl", sa.Float(), nullable=False),
        sa.Column("atl", sa.Float(), nullable=False),
        sa.Column("tsb", sa.Float(), nullable=False),
        sa.Column("methods", postgresql.JSONB(), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("athlete_id", "date", "sport", name="uq_daily_load_athlete_date_sport"),
    )


def downgrade() -> None:
    op.drop_table("daily_load")
