"""WA-3 RED tests: the ``message_log`` table (§6).

§6: "message_log" records inbound/outbound WhatsApp messages, tool calls
and a trace id. Contract pinned here:

- a row round-trips every column (direction, message_sid, body, tool
  name, JSONB payload, trace id, agent_version provenance, created_at);
- the UNIQUE constraint on ``message_sid`` is the IDEMPOTENCY ANCHOR: a
  Twilio retry of the same MessageSid cannot insert a second inbound row
  (IntegrityError), so the pipeline can never double-process it;
- rows without a ``message_sid`` (outbound messages, tool calls) are NOT
  constrained: Postgres unique constraints admit multiple NULLs, so many
  outbound/tool rows may share one conversation;
- ``agent_version`` carries engine_version-style provenance (§6), never
  NULL.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageLogRow

pytestmark = pytest.mark.anyio


async def test_message_log_row_round_trip(db_session: AsyncSession) -> None:
    row = MessageLogRow(
        direction="inbound",
        message_sid="SMabc123",
        body="¿cómo estoy de forma?",
        trace_id="trace-1",
        agent_version="0.1.0",
    )
    db_session.add(row)
    await db_session.commit()

    fetched = (await db_session.execute(select(MessageLogRow))).scalar_one()
    assert fetched.direction == "inbound"
    assert fetched.message_sid == "SMabc123"
    assert fetched.body == "¿cómo estoy de forma?"
    assert fetched.tool_name is None
    assert fetched.payload is None
    assert fetched.trace_id == "trace-1"
    assert fetched.agent_version == "0.1.0"
    assert isinstance(fetched.created_at, datetime)
    assert fetched.created_at.tzinfo is not None


async def test_message_sid_unique_is_the_idempotency_anchor(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        MessageLogRow(
            direction="inbound", message_sid="SMabc123", body="hola", trace_id="t1",
            agent_version="0.1.0",
        )
    )
    await db_session.commit()

    db_session.add(
        MessageLogRow(
            direction="inbound", message_sid="SMabc123", body="hola (retry)",
            trace_id="t2", agent_version="0.1.0",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_outbound_and_tool_rows_need_no_message_sid(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            MessageLogRow(
                direction="outbound", body="Tu CTL es 45.", trace_id="t1",
                agent_version="0.1.0",
            ),
            MessageLogRow(
                direction="outbound", body="Y tu TSB -5.", trace_id="t1",
                agent_version="0.1.0",
            ),
            MessageLogRow(
                direction="tool_call", tool_name="get_load_status",
                payload={"input": {"date_range": "7d"}, "output": {"status": "insufficient_data"}},
                trace_id="t1", agent_version="0.1.0",
            ),
        ]
    )
    await db_session.commit()

    rows = (await db_session.execute(select(MessageLogRow))).scalars().all()
    assert {row.direction for row in rows} == {"outbound", "tool_call"}
    assert all(row.message_sid is None for row in rows)


async def test_trace_id_and_direction_are_required(db_session: AsyncSession) -> None:
    db_session.add(MessageLogRow(agent_version="0.1.0"))  # no direction, no trace
    with pytest.raises(IntegrityError):
        await db_session.commit()
