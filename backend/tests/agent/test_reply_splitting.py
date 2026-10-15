"""WA-8 (second half) RED tests: the WhatsApp reply split at the
~1,000-character per-message budget (§9.2).

**Where the split happens — decision and why.** The split is enforced at
the CHARACTER budget (``settings.agent_whatsapp_message_char_limit``,
default 1000), NOT at WhatsApp's actual UTF-8 byte limit:

- The ODD constraint (§9.2) is stated in characters ("max ~1,000
  characters per message"), so the budget the owner asked for is a
  character budget.
- The API limit that actually matters on this path — Twilio's documented
  cap for a WhatsApp message (1,600 characters) — is likewise enforced
  in characters by Twilio, so counting ``len()`` characters is the
  correct unit end to end.
- Counting UTF-8 bytes would needlessly fragment Spanish text (accents
  and emoji are multi-byte) while never protecting anything the
  character count misses: 1,000 characters is always ≤ 1,000 bytes
  equivalent in transmission cost and well inside Twilio's cap.

Pinned behaviour:

- ``split_reply`` splits a longer reply into consecutive chunks, each
  within the limit, whose CONCATENATION reproduces the original text
  exactly — never truncated, never dropping the tail. Breaks prefer the
  last paragraph boundary in the window, then a line break, then a
  space; only separator-free text is hard-cut.
- The pipeline sends each chunk as a real, ordered Twilio message and
  records ONE outbound audit row per send (each send is a real message),
  ordered, with ``part``/``total`` provenance in the payload.
- A short reply is still exactly one send and one audit row.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message, split_reply
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMspl00000000000000000000000001"
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
    """One deterministic paragraph of ``length`` characters."""
    line = f"{seed} " + ("contenido " * (length // 10 + 1))
    return line[:length].rstrip()


def _long_reply() -> str:
    """A reply over the budget: three ~450-char paragraphs."""
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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    yield factory
    tools.reset_session_factory_provider()
    await engine.dispose()


@pytest.fixture
def session_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    from app.agent.session_store import DbSessionRepository
    from tests.dbsupport import test_database_url

    return DbSessionRepository(test_database_url())


class TestSplitReply:
    def test_short_reply_is_one_chunk(self) -> None:
        assert split_reply("Respuesta corta.", limit=LIMIT) == ["Respuesta corta."]

    def test_exactly_at_the_limit_is_one_message(self) -> None:
        text = "x" * LIMIT
        assert split_reply(text, limit=LIMIT) == [text]

    def test_splits_at_paragraph_boundaries_and_preserves_content(self) -> None:
        text = _long_reply()
        chunks = split_reply(text, limit=LIMIT)

        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= LIMIT
        # Never truncated, never dropped: the chunks, concatenated,
        # reproduce the original exactly.
        assert "".join(chunks) == text
        # Paragraph breaks are preferred: every chunk but possibly the
        # last ends at a paragraph boundary (separators ride on the
        # chunk they end).
        for chunk in chunks[:-1]:
            assert chunk.endswith("\n\n") or chunk.endswith("\n") or chunk.endswith(" ")

    def test_single_newline_fallback(self) -> None:
        text = "\n".join(_paragraph(f"l{i}", 400) for i in range(4))
        chunks = split_reply(text, limit=LIMIT)

        assert len(chunks) >= 2
        assert all(len(chunk) <= LIMIT for chunk in chunks)
        assert "".join(chunks) == text

    def test_hard_cut_when_text_has_no_separator(self) -> None:
        text = "x" * 2500
        chunks = split_reply(text, limit=LIMIT)

        assert chunks == ["x" * LIMIT, "x" * LIMIT, "x" * 500]
        assert "".join(chunks) == text

    def test_non_positive_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            split_reply("hola", limit=0)


class TestPipelineSplitsTheReply:
    async def test_long_reply_is_split_into_ordered_audited_sends(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """A reply over the budget goes out as N consecutive messages,
        each within the limit, with the full content preserved when the
        messages are concatenated — and ONE audit row per send."""
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

        # N ordered sends, each within the limit, content preserved.
        bodies = [call["body"] for call in twilio.messages.calls]
        assert len(bodies) >= 2
        for body in bodies:
            assert len(body) <= LIMIT
        assert "".join(bodies) == reply

        # ONE outbound audit row per send, in send order, with part/total.
        async with session_factory() as session:
            rows = (await session.execute(
                select(MessageLogRow).where(MessageLogRow.direction == "outbound")
            )).scalars().all()
        rows = sorted(rows, key=lambda r: r.id)
        assert len(rows) == len(bodies)
        assert [row.body for row in rows] == bodies
        totals = {row.payload["total"] for row in rows}
        assert totals == {len(bodies)}
        assert [row.payload["part"] for row in rows] == list(range(1, len(bodies) + 1))
        # Every send is a real message sharing the turn's provenance.
        assert all(row.trace_id == rows[0].trace_id for row in rows)

        # Exactly one inbound row: the message itself.
        async with session_factory() as session:
            inbound = (await session.execute(
                select(MessageLogRow).where(MessageLogRow.direction == "inbound")
            )).scalars().all()
        assert len(inbound) == 1

    async def test_short_reply_is_exactly_one_send(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        model = FakeModel(["Respuesta corta."])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": "hola"},
            settings=make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        assert len(twilio.messages.calls) == 1
        assert twilio.messages.calls[0]["body"] == "Respuesta corta."
        async with session_factory() as session:
            rows = (await session.execute(
                select(MessageLogRow).where(MessageLogRow.direction == "outbound")
            )).scalars().all()
        assert len(rows) == 1
