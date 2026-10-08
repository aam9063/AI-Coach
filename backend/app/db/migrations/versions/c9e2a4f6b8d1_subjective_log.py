"""subjective_log: the owner's daily subjective report (WA-6, §9.3/§7.4)

Adds the ``subjective_log`` table: one row per (athlete, date) with the
athlete's own report from the WhatsApp conversation — ``rpe`` (scale
1-10, same scale as ``activity.rpe``/``icu_rpe``), ``fatigue`` (1-10:
1 = none, 10 = extreme), ``soreness`` (1-10) and free-text ``notes``.

This is OWNER-ENTERED INPUT DATA (the ``activity.rpe`` LOAD-12
precedent), not an engine output: no ``engine_version`` column — the
audit stamp is ``recorded_at``. The unique constraint
``uq_subjective_log_athlete_date`` is the idempotency anchor: logging
twice the same day updates the row, never duplicates it.

The readiness engine consumes the report as its "subjective fatigue"
context signal (§7.4): ``app.services.readiness`` reads the day's row
and passes ``subjective_fatigue_reported = (fatigue IS NOT NULL)`` to
``app.engine.readiness.readiness_assessment`` — the engine's BOOL
semantics, never a new one. Out-of-range values are rejected by the
tool with a reportable reason, never stored silently (LOAD-12).

NEW revision revising the applied head ``d1b9c8e2a4f6`` (migration
hygiene: never edit an already-applied migration in place).

Revision ID: c9e2a4f6b8d1
Revises: d1b9c8e2a4f6
Create Date: 2026-10-08 09:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e2a4f6b8d1"
down_revision: str | None = "d1b9c8e2a4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subjective_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "rpe",
            sa.Float(),
            nullable=True,
            comment=(
                "OWNER-ENTERED INPUT (§9.3): the athlete's own reported "
                "RPE, scale 1-10 (same scale as activity.rpe/icu_rpe). "
                "NULL = not reported; out-of-range values are rejected "
                "by the tool, never stored silently (LOAD-12)."
            ),
        ),
        sa.Column(
            "fatigue",
            sa.Integer(),
            nullable=True,
            comment=(
                "OWNER-ENTERED INPUT (§9.3): reported fatigue, 1-10 "
                "(1 = none, 10 = extreme). NULL = not reported. The "
                "readiness engine consumes this as a BOOL context "
                "signal (subjective fatigue, §7.4)."
            ),
        ),
        sa.Column(
            "soreness",
            sa.Integer(),
            nullable=True,
            comment=(
                "OWNER-ENTERED INPUT (§9.3): reported soreness, 1-10 "
                "(1 = none, 10 = extreme). NULL = not reported."
            ),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "athlete_id", "date", name="uq_subjective_log_athlete_date"
        ),
    )


def downgrade() -> None:
    op.drop_table("subjective_log")
