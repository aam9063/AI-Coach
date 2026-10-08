"""message_log: WhatsApp conversation log (WA-3, brief §6)

Adds the ``message_log`` table recording inbound/outbound WhatsApp
messages, agent tool calls and a trace id (brief §6), so every reply is
auditable back to the tool outputs it explains (§3).

- ``direction``: "inbound" | "outbound" | "tool_call".
- ``message_sid``: the Twilio MessageSid of inbound messages. The UNIQUE
  constraint ``uq_message_log_message_sid`` is the IDEMPOTENCY ANCHOR: a
  Twilio retry of the same message cannot produce a second inbound row,
  so the Celery pipeline can never double-process (or double-reply) one
  message. Outbound/tool rows leave it NULL; Postgres unique constraints
  admit multiple NULLs, so one conversation holds many of them.
- ``trace_id`` groups every row written by one processing turn.
- ``agent_version`` is the engine_version-style provenance (§6) for the
  agent pipeline that produced the turn (messages/tool calls are not
  engine outputs, so the column is named for the agent).

NEW revision revising the applied head ``f0a1b2c3d4e5`` (migration
hygiene: never edit an already-applied migration in place).

Revision ID: d1b9c8e2a4f6
Revises: f0a1b2c3d4e5
Create Date: 2026-10-08 09:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1b9c8e2a4f6"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("message_sid", sa.String(length=64), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_sid", name="uq_message_log_message_sid"),
    )
    op.create_index(
        op.f("ix_message_log_trace_id"), "message_log", ["trace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_log_trace_id"), table_name="message_log")
    op.drop_table("message_log")
