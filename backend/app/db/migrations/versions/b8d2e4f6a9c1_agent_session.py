"""agent_session: conversation memory tables (WA-8, §9.2)

Adds the four tables behind the Strands SDK's own session seam — the
pipeline persists each conversation through a
``RepositorySessionManager`` whose ``SessionRepository`` is OUR
implementation (:mod:`app.agent.session_store`) backed by these tables:

- ``agent_session``: one SDK ``Session`` row per conversation. The
  session id is the SENDER (the WhatsApp ``From``), so memory is per
  sender by construction — one sender's history can never leak into
  another's session — and a message processed by a different Celery
  process reloads the earlier turns from the database. The unique
  constraint on ``session_id`` makes concurrent session creation by two
  worker processes idempotent.
- ``agent_session_agent``: the SDK ``SessionAgent`` per (session, agent),
  including the conversation manager state that carries the ROLLING
  SUMMARY message and the folded-turn count.
- ``agent_session_message``: one SDK ``SessionMessage`` per conversation
  message, with the SDK's sequential ``message_id``. The unique
  (session_id, agent_id, message_id) anchor makes the SDK's per-message
  writes idempotent; ``list_messages`` paginates by that index with an
  offset (the summarized prefix is skipped there, never deleted — the
  verbatim history stays in the database).
- ``agent_conversation_summary``: the STORED rolling summary of a
  conversation (last N turns verbatim, older turns collapsed). Generated
  ONCE per OWNER-CHOICE threshold crossing and then reused — never
  regenerated on every message. Its generation is a real model
  invocation, so ``usage`` records its token cost (counted against the
  conversation budget, WA-5) and ``trace_id``/``agent_version`` carry the
  same provenance as any turn; ``covered_message_count`` anchors what it
  replaced.

These are session-management state, NOT engine outputs and NOT the
``message_log`` audit trail (which is untouched): no ``engine_version``
column; the agent-pipeline provenance columns live on the summary row
only.

NEW revision on top of the applied head ``c9e2a4f6b8d1`` (migration
hygiene: never edit an already-applied migration in place).

Revision ID: b8d2e4f6a9c1
Revises: c9e2a4f6b8d1
Create Date: 2026-10-08 14:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "b8d2e4f6a9c1"
down_revision: str | None = "c9e2a4f6b8d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "session_id",
            sa.String(length=255),
            nullable=False,
            comment="SDK session id — the per-sender conversation key "
            "(the WhatsApp From).",
        ),
        sa.Column("session_type", sa.String(length=16), nullable=False),
        sa.Column(
            "data",
            JSONB(),
            nullable=False,
            comment="Full SDK Session serialization (Session.to_dict()).",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_agent_session_session_id"),
    )
    op.create_table(
        "agent_session_agent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column(
            "data",
            JSONB(),
            nullable=False,
            comment="Full SDK SessionAgent serialization "
            "(SessionAgent.to_dict()), including the conversation manager "
            "state (rolling summary + folded-turn count).",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "agent_id", name="uq_agent_session_agent"),
    )
    op.create_index(
        "ix_agent_session_agent_session_id",
        "agent_session_agent",
        ["session_id"],
    )
    op.create_table(
        "agent_session_message",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column(
            "message_id",
            sa.Integer(),
            nullable=False,
            comment="The SDK's sequential index in the conversation history.",
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "data",
            JSONB(),
            nullable=False,
            comment="Full SDK SessionMessage serialization.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "agent_id", "message_id", name="uq_agent_session_message"
        ),
    )
    op.create_index(
        "ix_agent_session_message_session_id",
        "agent_session_message",
        ["session_id"],
    )
    op.create_index(
        "ix_agent_session_message_agent_id",
        "agent_session_message",
        ["agent_id"],
    )
    op.create_table(
        "agent_conversation_summary",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "session_id",
            sa.String(length=255),
            nullable=False,
            comment="The per-sender conversation key "
            "(agent_session.session_id).",
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=False,
            comment="The summary text handed to the model with the next turns.",
        ),
        sa.Column(
            "covered_message_count",
            sa.Integer(),
            nullable=False,
            comment="How many original messages the summary replaced "
            "when generated.",
        ),
        sa.Column(
            "usage",
            JSONB(),
            nullable=True,
            comment="The summary invocation's own token usage (inputTokens/"
            "outputTokens/totalTokens) — counted against the conversation "
            "budget.",
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_conversation_summary_session_id",
        "agent_conversation_summary",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_conversation_summary")
    op.drop_table("agent_session_message")
    op.drop_table("agent_session_agent")
    op.drop_table("agent_session")
