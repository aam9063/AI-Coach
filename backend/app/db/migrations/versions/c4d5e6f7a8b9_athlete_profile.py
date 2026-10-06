"""athlete_profile and append-only threshold history (ZON-10, §6/§7.3)

Adds the two tables ZON-10 owns:

- ``athlete_profile``: the athlete's CURRENT thresholds with per-metric
  provenance (``*_source`` columns holding the engine's machine-readable
  source keys, ZON-3) and ``engine_version`` (§6: every persisted engine
  output carries it). The identity decision is documented on the model
  (``app.db.models.AthleteProfileRow``): the local single-athlete integer
  ``athlete_id`` (default 1) matches the existing ``wellness.athlete_id`` /
  ``daily_load.athlete_id`` convention — those columns are deliberately NOT
  rewritten and get no FK — while the external Intervals.icu identity is
  the string ``intervals_athlete_id`` (real ids carry an ``i`` prefix, e.g.
  ``i555003``).
- ``athlete_threshold_history``: APPEND-ONLY log of threshold proposals and
  the owner's decisions, carrying the prior value, the proposed value, the
  value actually applied (NULL when declined), the evidence that justified
  the change (§7.3: a proposal carries evidence and the prior value), the
  confirming actor and ``engine_version``. Rows are never updated or
  deleted: declined proposals are recorded WITHOUT touching the profile,
  which is what keeps the update human-in-the-loop.

**Why this is a separate revision rather than an edit of the previous
head**: an earlier attempt added these tables to the already-applied
revision ``b2f8d4c6a9e1`` in place. Any environment stamped with that
revision — the local development database included — would then report
"up to date" while silently missing the tables. Migrations are immutable
once applied; new schema means a new revision, so a plain
``alembic upgrade head`` creates these tables everywhere.

Revision ID: c4d5e6f7a8b9
Revises: b2f8d4c6a9e1
Create Date: 2026-10-07 09:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b2f8d4c6a9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "athlete_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "athlete_id",
            sa.Integer(),
            nullable=False,
            unique=True,
            comment=(
                "Local single-athlete integer, matching the existing "
                "wellness.athlete_id / daily_load.athlete_id convention "
                "(default 1). Those pre-existing integer columns are not "
                "rewritten and get no FK; this row is the profile they "
                "refer to."
            ),
        ),
        sa.Column(
            "intervals_athlete_id",
            sa.String(length=32),
            nullable=True,
            unique=True,
            comment=(
                "External Intervals.icu athlete id (string, e.g. "
                "'i555003' — real ids carry an 'i' prefix); the join key "
                "to the athlete's Intervals identity (ZON-10 decision)."
            ),
        ),
        sa.Column("ftp_watts", sa.Float(), nullable=True),
        sa.Column(
            "ftp_source",
            sa.String(length=32),
            nullable=True,
            comment=(
                "Provenance: the engine's machine-readable source key "
                "(manual / cp_derived / twenty_min_power, ZON-3; plus the "
                "fitting-function names). Nullable = not established."
            ),
        ),
        sa.Column("cp_watts", sa.Float(), nullable=True),
        sa.Column("cp_source", sa.String(length=32), nullable=True),
        sa.Column("w_prime_joules", sa.Float(), nullable=True),
        sa.Column("cs_mps", sa.Float(), nullable=True),
        sa.Column("cs_source", sa.String(length=32), nullable=True),
        sa.Column("d_prime_meters", sa.Float(), nullable=True),
        sa.Column("css_mps", sa.Float(), nullable=True),
        sa.Column("css_source", sa.String(length=32), nullable=True),
        sa.Column(
            "engine_version",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Engine version that last wrote this row (§6: every "
                "persisted engine output carries the engine version)."
            ),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "athlete_threshold_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "athlete_id",
            sa.Integer(),
            nullable=False,
            comment=(
                "Same local single-athlete integer as athlete_profile "
                "(no FK — same convention as wellness/daily_load)."
            ),
        ),
        sa.Column(
            "metric",
            sa.String(length=32),
            nullable=False,
            comment="One of app.engine.zones.THRESHOLD_METRIC_KEYS.",
        ),
        sa.Column(
            "decision",
            sa.String(length=16),
            nullable=False,
            comment="'accepted' or 'declined' (validated by the flow).",
        ),
        sa.Column("prior_value", sa.Float(), nullable=True),
        sa.Column(
            "new_value",
            sa.Float(),
            nullable=True,
            comment=(
                "The value actually applied on acceptance; NULL when the "
                "proposal was declined."
            ),
        ),
        sa.Column(
            "proposed_value",
            sa.Float(),
            nullable=False,
            comment="The proposal's proposed value (always present).",
        ),
        sa.Column(
            "evidence",
            sa.Text(),
            nullable=False,
            comment="The proposal's evidence text (§7.3 justification).",
        ),
        sa.Column(
            "confirmed_by",
            sa.String(length=64),
            nullable=False,
            comment=(
                "The confirming actor (e.g. 'owner_whatsapp' once the "
                "Feature-6 WhatsApp interaction lands)."
            ),
        ),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column(
            "engine_version",
            sa.String(length=32),
            nullable=False,
            comment="§6: every persisted engine output carries it.",
        ),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        comment=(
            "APPEND-ONLY history of threshold proposals and decisions "
            "(ZON-10, §6/§7.3); rows are never updated or deleted."
        ),
    )
    op.create_index(
        "ix_athlete_threshold_history_athlete_id",
        "athlete_threshold_history",
        ["athlete_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_athlete_threshold_history_athlete_id",
        table_name="athlete_threshold_history",
    )
    op.drop_table("athlete_threshold_history")
    op.drop_table("athlete_profile")
