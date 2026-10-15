"""Audit-trail RED tests: every outbound row's payload records the text
that was actually sent.

Live debugging (2026-10-14) needed "what did we send?" from ``message_log``
alone and could not answer it: the outbound payload carried ``to``,
``usage``, ``latency_ms`` and ``part``/``total`` but not the reply text, so
the SDK's session store had to be read — twice — and once that read raced
the turn and showed a stale reply. An audit trail must answer "what did we
send?" by itself.

Pinned contract:

- EVERY outbound row's payload carries the exact sent text under
  ``text``, per message (the budget-exhausted reply included);
- for a split reply each chunk's payload carries its own text next to its
  ``part``/``total``, so concatenating the payloads' texts in send order
  reproduces the full answer exactly;
- existing payload keys are untouched, one audit row per send still holds,
  and a duplicate-``message_sid`` retry still performs zero invocations
  and zero sends (pinned by tests/scheduler/test_whatsapp_pipeline.py);
- this is audit data only: nothing feeds it back into the model or the
  conversation memory (the pipeline never reads it back).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMtxt00000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"
LIMIT = 1000


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        agent_whatsapp_message_char_limit=LIMIT,
        **overrides,
    )


def _paragraph(seed: str, length: int) -> str:
    line = f"{seed} " + ("contenido " * (length // 10 + 1))
    return line[:length].rstrip()


def _long_reply() -> str:
    return "\n\n".join(_paragraph(f"párrafo {i}", 450) for i in range(3))


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
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def session_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    from app.agent.session_store import DbSessionRepository
    from tests.dbsupport import test_database_url

    return DbSessionRepository(test_database_url())


async def _outbound_rows(
    session_factory: async_sessionmaker[Any],
) -> list[MessageLogRow]:
    async with session_factory() as session:
        rows = (await session.execute(
            select(MessageLogRow).where(MessageLogRow.direction == "outbound")
        )).scalars().all()
    return sorted(rows, key=lambda r: r.id)


class TestOutboundPayloadRecordsSentText:
    async def test_single_message_turn_records_the_exact_sent_text(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """A single-message turn's outbound payload carries the text that
        was actually sent — verbatim, not a projection of it."""
        reply = "Tu CTL es 45.0 y tu TSB -2.1."
        model = FakeModel([reply])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": "¿cómo estoy?"},
            settings=make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        rows = await _outbound_rows(session_factory)
        assert len(rows) == 1
        assert (rows[0].payload or {})["text"] == reply
        # It is exactly what went out on the wire and what the row body
        # records — three projections of one send, all identical.
        assert twilio.messages.calls[0]["body"] == reply
        assert rows[0].body == reply

    async def test_split_reply_chunks_carry_their_own_text(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """For a split reply each chunk's payload carries its own text next
        to its part/total; concatenating the payloads' texts in send order
        reproduces the full answer exactly."""
        reply = _long_reply()
        model = FakeModel([reply])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": "cuéntame todo"},
            settings=make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        rows = await _outbound_rows(session_factory)
        assert len(rows) >= 2
        assert "".join((row.payload or {})["text"] for row in rows) == reply
        # Per-message fidelity: each payload's text is that row's send,
        # matching the Twilio call in the same position.
        for row, call in zip(rows, twilio.messages.calls, strict=True):
            assert (row.payload or {})["text"] == call["body"]
            assert (row.payload or {})["part"] <= (row.payload or {})["total"]
        assert {(row.payload or {})["total"] for row in rows} == {len(rows)}

    async def test_budget_exhausted_reply_also_records_its_text(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EVERY outbound row records the sent text — the explicit
        budget-exhausted message included."""
        from app.agent import budget

        async def exhausted_usage(session: Any, sender: str) -> int:
            return 10 ** 9  # far past any configured budget

        monkeypatch.setattr("app.agent.pipeline.conversation_usage_tokens", exhausted_usage)
        model = FakeModel(["no debería invocarse"])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": "hola"},
            settings=make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        rows = await _outbound_rows(session_factory)
        assert len(rows) == 1
        assert (rows[0].payload or {})["budget_exhausted"] is True
        assert (rows[0].payload or {})["text"] == budget.BUDGET_EXHAUSTED_REPLY
        assert twilio.messages.calls[0]["body"] == budget.BUDGET_EXHAUSTED_REPLY
