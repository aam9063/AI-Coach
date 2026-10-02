"""activity.source_id Integer -> String(32)

Intervals.icu activity ids are strings with an ``i`` prefix (live
API-verified, e.g. ``i163428838``); the Integer column rejects every real
id. The (source, source_id) unique constraint ``uq_activity_source_source_id``
is preserved — PostgreSQL rebuilds its index automatically during the
ALTER COLUMN TYPE and keeps the constraint name.

Revision ID: b7e4c9a1d2f3
Revises: daa3ba6946b9
Create Date: 2026-02-14 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4c9a1d2f3"
down_revision: str | None = "daa3ba6946b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "activity",
        "source_id",
        existing_type=sa.Integer(),
        type_=sa.String(length=32),
        existing_nullable=False,
        postgresql_using="source_id::text",
    )


def downgrade() -> None:
    op.alter_column(
        "activity",
        "source_id",
        existing_type=sa.String(length=32),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="source_id::integer",
    )
