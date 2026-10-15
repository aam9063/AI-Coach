"""threshold_proposal: pending threshold proposals awaiting owner review (WA-6)

Adds the ``threshold_proposal`` table: one APPEND-ONLY row per threshold
proposal raised from the conversation by the ``propose_threshold_update``
tool (brief §9.3 "pending proposal awaiting athlete confirmation", §7.3
"propose (not apply) an update").

A threshold change is the OWNER'S decision, never the agent's: the tool
persists the proposal here and returns a result stating that NO threshold
was changed — ``athlete_profile`` and the owner configuration the engine
reads are untouched by a proposal. ``status`` starts ``"pending"`` with a
SERVER-SIDE DEFAULT so even a non-ORM writer cannot forget it; only the
owner-driven application step (a later feature) would move it, recording
the decision in ``athlete_threshold_history`` (ZON-10).

This is agent-channel data, not an engine output: ``engine_version``
records the engine version the proposing turn ran under (§6 provenance
of the conversation) and ``created_at`` stamps the proposal.

NEW revision revising the applied head ``d2c4e6f8a0b2`` (migration
hygiene: never edit an already-applied migration in place).

Revision ID: e7c9a1b3d5f7
Revises: d2c4e6f8a0b2
Create Date: 2026-10-15 09:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c9a1b3d5f7"
down_revision: str | None = "d2c4e6f8a0b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "threshold_proposal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column(
            "sport",
            sa.String(length=32),
            nullable=False,
            comment=(
                "Normalized sport name (ride/run/swim...) as proposed by "
                "the owner through the conversation."
            ),
        ),
        sa.Column(
            "metric",
            sa.String(length=32),
            nullable=False,
            comment=(
                "The engine zone-model key the proposal targets "
                "(bike_power/run_hr/swim_pace) — the same vocabulary as "
                "the zone tools."
            ),
        ),
        sa.Column(
            "proposed_value",
            sa.Float(),
            nullable=False,
            comment="The proposed threshold value, in the metric's unit.",
        ),
        sa.Column(
            "unit",
            sa.String(length=16),
            nullable=False,
            comment="Unit of proposed_value (watts/bpm/m/s).",
        ),
        sa.Column(
            "evidence",
            sa.Text(),
            nullable=False,
            comment=(
                "The §7.3 justification: what effort or state motivates "
                "the proposal."
            ),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
            comment=(
                "'pending' until the owner-driven application flow (a "
                "later feature) records the decision in "
                "athlete_threshold_history."
            ),
        ),
        sa.Column(
            "engine_version",
            sa.String(length=32),
            nullable=False,
            comment=(
                "The engine version the proposing turn ran under (§6 "
                "provenance of the conversation)."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="When the proposal was raised.",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("threshold_proposal")
