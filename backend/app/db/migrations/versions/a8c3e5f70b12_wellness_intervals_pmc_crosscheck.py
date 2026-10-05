"""wellness Intervals.icu PMC cross-check columns (LOAD-10, §12.3)

Adds two nullable ``Float`` columns to ``wellness``:

- ``intervals_icu_ctl``
- ``intervals_icu_atl``

These store the Performance Manager values (CTL/ATL) computed by
Intervals.icu itself so our engine's PMC (``app.engine.pmc``) can be
cross-checked against them for the same load inputs (§12.3: "PMC values
within an agreed tolerance of Intervals.icu for the same data"). They are
explicitly NON-AUTHORITATIVE (§5.1): the column names name the source and
nothing in the engine or services may consume them as training state.

Nullable because the values only exist where the source reports them; NULL
means "not reported", never a silent 0.

Revision ID: a8c3e5f70b12
Revises: e5f6a7b8c9d0
Create Date: 2026-10-06 09:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c3e5f70b12"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wellness",
        sa.Column(
            "intervals_icu_ctl",
            sa.Float(),
            nullable=True,
            comment=(
                "NON-AUTHORITATIVE (§5.1): Intervals.icu's own CTL, kept only "
                "to cross-check our engine's PMC (§12.3); never training truth."
            ),
        ),
    )
    op.add_column(
        "wellness",
        sa.Column(
            "intervals_icu_atl",
            sa.Float(),
            nullable=True,
            comment=(
                "NON-AUTHORITATIVE (§5.1): Intervals.icu's own ATL, kept only "
                "to cross-check our engine's PMC (§12.3); never training truth."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("wellness", "intervals_icu_atl")
    op.drop_column("wellness", "intervals_icu_ctl")
