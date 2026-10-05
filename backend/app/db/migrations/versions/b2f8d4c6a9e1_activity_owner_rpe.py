"""activity.rpe: owner-entered session RPE (LOAD-12, §5.1)

Adds one nullable ``Float`` column ``rpe`` to ``activity``.

This column is OWNER-ENTERED INPUT DATA, not a computed metric: it stores
the athlete's own session RPE as entered in Intervals.icu (``icu_rpe``,
integer scale 1-10, editable on the activity page; payload aliases
``session_rpe`` and ``perceived_exertion``). Unlike the explicitly
non-authoritative ``intervals_icu_*`` cross-check columns (§5.1), the
engine CONSUMES this value — the sRPE method (Foster et al. 2001) for
strength sports is preferred over the HR path whenever it is present,
because HR is not a valid strength-load proxy (gym sessions average
81-103 bpm).

Nullable because the value only exists where the owner entered one; NULL
means "not entered", never a silent 0. Values outside 1-10 are rejected
at the ingest mapping layer with a reportable reason and never stored.

Revision ID: b2f8d4c6a9e1
Revises: a8c3e5f70b12
Create Date: 2026-10-07 09:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2f8d4c6a9e1"
down_revision: str | None = "a8c3e5f70b12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "activity",
        sa.Column(
            "rpe",
            sa.Float(),
            nullable=True,
            comment=(
                "OWNER-ENTERED INPUT (LOAD-12): the athlete's own session "
                "RPE as entered in Intervals.icu (icu_rpe, scale 1-10); "
                "consumed by the engine's sRPE method for strength sports. "
                "NULL means not entered. Not a computed metric and not a "
                "non-authoritative cross-check value (§5.1)."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("activity", "rpe")
