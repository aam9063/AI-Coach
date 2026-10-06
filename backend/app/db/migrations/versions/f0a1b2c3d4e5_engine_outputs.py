"""readiness_snapshot, weekly_intensity, session_durability (RID-10)

Persistence of the readiness / intensity / durability engine outputs with
``engine_version`` on every row (brief §6: "every engine output persisted
to the DB carries engine_version so results can be recomputed").

- ``readiness_snapshot``: one structured multi-signal readiness row per
  (athlete_id, date); the §7.4 signal objects persist as JSONB, the
  warning-rule result as the headline columns ``agreement_count`` /
  ``suggest_reduce_intensity`` plus the evidence (adverse keys, reasons,
  context inputs). Unique key ``uq_readiness_snapshot_athlete_date`` is
  the idempotency anchor.
- ``weekly_intensity``: one 3-zone distribution row per (athlete_id,
  iso_year, iso_week, sport); a ``no_data`` sport-week persists with
  ``percentages IS NULL`` (never a fabricated 0% split). Unique key
  ``uq_weekly_intensity_athlete_year_week_sport``.
- ``session_durability``: one EF/decoupling row per activity (FK to
  ``activity.id``, CASCADE); a ``not_steady`` session persists with NULL
  metrics and the measured drift. Unique key
  ``uq_session_durability_activity``.

NEW revision revising the applied head ``c4d5e6f7a8b9`` (migration
hygiene: never edit an already-applied migration in place).

Revision ID: f0a1b2c3d4e5
Revises: c4d5e6f7a8b9
Create Date: 2026-10-05 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "readiness_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("adverse_signal_keys", postgresql.JSONB(), nullable=False),
        sa.Column("agreement_count", sa.Integer(), nullable=False),
        sa.Column("suggest_reduce_intensity", sa.Boolean(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("tsb", sa.Float(), nullable=False),
        sa.Column("tsb_very_negative_below", sa.Float(), nullable=False),
        sa.Column("subjective_fatigue_reported", sa.Boolean(), nullable=True),
        sa.Column("acwr", sa.Float(), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "athlete_id", "date", name="uq_readiness_snapshot_athlete_date"
        ),
    )
    op.create_table(
        "weekly_intensity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("iso_year", sa.Integer(), nullable=False),
        sa.Column("iso_week", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("sport", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("z1_seconds", sa.Float(), nullable=False),
        sa.Column("z2_seconds", sa.Float(), nullable=False),
        sa.Column("z3_seconds", sa.Float(), nullable=False),
        sa.Column("total_seconds", sa.Float(), nullable=False),
        sa.Column("percentages", postgresql.JSONB(), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "athlete_id",
            "iso_year",
            "iso_week",
            "sport",
            name="uq_weekly_intensity_athlete_year_week_sport",
        ),
    )
    op.create_table(
        "session_durability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "activity_id",
            sa.Integer(),
            sa.ForeignKey("activity.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ef_first_half", sa.Float(), nullable=True),
        sa.Column("ef_second_half", sa.Float(), nullable=True),
        sa.Column("decoupling", sa.Float(), nullable=True),
        sa.Column("decoupling_pct", sa.Float(), nullable=True),
        sa.Column("within_reference_band", sa.Boolean(), nullable=True),
        sa.Column("reference_band", sa.Float(), nullable=False),
        sa.Column("intensity_first_half", sa.Float(), nullable=True),
        sa.Column("intensity_second_half", sa.Float(), nullable=True),
        sa.Column("intensity_drift", sa.Float(), nullable=False),
        sa.Column("max_intensity_drift", sa.Float(), nullable=False),
        sa.Column("n_samples", sa.Integer(), nullable=False),
        sa.Column("n_first_half", sa.Integer(), nullable=False),
        sa.Column("n_second_half", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", name="uq_session_durability_activity"),
    )
    op.create_index(
        "ix_session_durability_activity_id",
        "session_durability",
        ["activity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_durability_activity_id", table_name="session_durability")
    op.drop_table("session_durability")
    op.drop_table("weekly_intensity")
    op.drop_table("readiness_snapshot")
