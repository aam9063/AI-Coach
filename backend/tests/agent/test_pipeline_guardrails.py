"""WA-5 RED tests: the guardrails wired around the SDK loop in the pipeline.

The tool-calling LOOP is the Strands Agents SDK's (message assembly, tool
execution, stop handling); ours are the guardrails this file pins:

- **Per-invocation limits**: ``Limits(turns=…, output_tokens=…,
  total_tokens=…)`` built from settings is passed on EVERY invocation; when
  the SDK loop trips a cap it stops with a ``limit_*`` stop reason and the
  athlete receives an explicit guardrail message instead of silence.
- **Per-conversation budget**: the usage of every turn (from
  ``result.metrics.accumulated_usage``) is persisted in ``message_log`` on
  the outbound row; the pipeline accumulates the conversation's usage (the
  WhatsApp free-form 24 h window, §9.1) and, when the budget is exhausted,
  stops BEFORE invoking the model and replies with an explicit message.
- **Usage auditability**: every turn's tokens and latency are persisted per
  turn in ``message_log`` (§6), so a turn's cost is auditable.

DB-backed (dedicated test database, skips cleanly without Postgres); the
fake model drives the real loop with zero network.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.budget import BUDGET_EXHAUSTED_REPLY
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMguard0000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"


def make_message(**overrides: str) -> dict[str, str]:
    return {"MessageSid": SID, "From": FROM, "To": TO, "Body": "¿cómo estoy?",
            **overrides}


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="",
        twilio_auth_token="",
        **overrides,
    )


class FakeTwilioMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, **kwargs: str) -> Any:
        self.calls.append(kwargs)
        return {"sid": "SMout"}


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeTwilioMessages()


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[Any], None]:
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; pipeline tests require the compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    yield factory
    tools.reset_session_factory_provider()
    await engine.dispose()


async def outbound_rows(factory: async_sessionmaker[Any]) -> list[MessageLogRow]:
    async with factory() as session:
        rows = (await session.execute(
            select(MessageLogRow).where(MessageLogRow.direction == "outbound")
        )).scalars().all()
    return sorted(rows, key=lambda r: r.id)


class TestPerInvocationLimits:
    async def test_loop_stops_at_the_turn_limit_and_replies_explicitly(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        """Two scripted tool-use turns + agent_max_turns=2: the SDK loop
        stops with ``limit_turns``, no final text exists, and the athlete
        still gets an explicit guardrail message — never an empty reply."""
        model = FakeModel(
            [{"name": "get_load_status", "input": {"date_range": "7d"}},
             {"name": "get_load_status", "input": {"date_range": "30d"}}]
        )
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(),
            settings=make_settings(agent_max_turns=2),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )

        # The loop stopped on OUR limit (no third model call happened).
        assert len(model.invocations) == 2
        assert twilio.messages.calls[0]["body"].strip() != ""
        assert "limits" in twilio.messages.calls[0]["body"].lower() or (
            "couldn't" in twilio.messages.calls[0]["body"].lower()
        )


class TestUsagePersistenceAndBudget:
    async def test_usage_and_latency_are_persisted_per_turn(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        model = FakeModel(["Respuesta directa."])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(),
            settings=make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )

        rows = await outbound_rows(session_factory)
        assert len(rows) == 1
        payload = rows[0].payload
        assert payload is not None
        assert payload["usage"] == {
            "inputTokens": 10, "outputTokens": 5, "totalTokens": 15,
        }
        assert payload["latency_ms"] == 3
        # The conversation key: the sender the reply went to.
        assert payload["to"] == FROM

    async def test_exhausted_budget_stops_before_invoking(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        """Prior conversation usage at/over the budget: the model is never
        invoked and the athlete gets the explicit budget message."""
        # Seed the conversation's prior usage (a prior turn, still inside
        # the 24 h window).
        async with session_factory() as session:
            session.add(MessageLogRow(
                direction="outbound", body="turn anterior",
                payload={"to": FROM, "usage": {"inputTokens": 100, "outputTokens": 100,
                                               "totalTokens": 100},
                         "latency_ms": 5},
                trace_id="prior-turn", agent_version="0.1.0",
            ))
            await session.commit()

        model = FakeModel([])  # would raise if the loop invoked the model
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(),
            settings=make_settings(agent_conversation_token_budget=100),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )

        assert len(model.invocations) == 0
        assert twilio.messages.calls == [
            {"to": FROM, "from_": TO, "body": BUDGET_EXHAUSTED_REPLY}
        ]
        rows = await outbound_rows(session_factory)
        assert len(rows) == 2  # prior seeded row + the budget message row

    async def test_budget_accumulates_across_turns_of_one_conversation(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        """Turn 1 runs and persists its usage; turn 2 of the SAME
        conversation sees the accumulated usage over the budget and stops."""
        settings = make_settings(agent_conversation_token_budget=15)
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(),
            settings=settings,
            session_factory=session_factory,
            model=FakeModel(["primera respuesta."]),
            twilio_client=twilio,
        )
        second_model = FakeModel([])  # would raise if invoked
        await process_inbound_message(
            make_message(MessageSid="SMguard0000000000000000000000002"),
            settings=settings,
            session_factory=session_factory,
            model=second_model,
            twilio_client=twilio,
        )

        assert len(twilio.messages.calls) == 2
        assert twilio.messages.calls[0]["body"] == "primera respuesta."
        assert twilio.messages.calls[1]["body"] == BUDGET_EXHAUSTED_REPLY
        assert len(second_model.invocations) == 0

    async def test_other_senders_usage_does_not_count(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        """The budget is per conversation: another sender's persisted usage
        must not exhaust this one."""
        async with session_factory() as session:
            session.add(MessageLogRow(
                direction="outbound", body="otra conversación",
                payload={"to": "whatsapp:+34999999999",
                         "usage": {"totalTokens": 999999}, "latency_ms": 1},
                trace_id="other", agent_version="0.1.0",
            ))
            await session.commit()

        model = FakeModel(["respuesta."])
        twilio = FakeTwilioClient()
        await process_inbound_message(
            make_message(),
            settings=make_settings(agent_conversation_token_budget=15),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )
        assert twilio.messages.calls[0]["body"] == "respuesta."
