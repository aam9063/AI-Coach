"""WA-8 (first half) RED tests: conversation memory that survives process
boundaries.

**What is the SDK's and what is ours** (ODD task, "Agent runtime" table:
``strands.session`` exposes ``RepositorySessionManager`` + ``SessionRepository``
explicitly because each Celery task is a separate process):

- The persistence MECHANISM is the SDK's session seam: a
  ``RepositorySessionManager`` keyed by the SENDER (one session per
  conversation — isolation by construction) backed by OUR
  ``SessionRepository`` implementation on the project's database.
- Ours is the rolling-summary policy: the last N turns stay verbatim,
  older turns collapse into a stored summary that is generated ONCE when
  the trigger threshold is crossed and then REUSED (never regenerated on
  every message), with the summary invocation's cost counted against the
  conversation budget and persisted with the same provenance as any turn.

Pinned behaviours (all DB-backed on the dedicated test database, fake
model driving the real SDK loop, zero network):

- two sequential pipeline runs on one conversation: the second
  invocation's input contains the first turn;
- the same across a FRESH session factory/engine and a fresh repository
  instance (proving the memory is not in-memory);
- the rolling summary is generated once and reused (asserted through the
  model invocation counts);
- the summary's token cost is counted in the conversation budget;
- one sender's history never leaks into another sender's conversation;
- a Twilio retry (duplicate ``message_sid``) still performs ZERO model
  invocations and ZERO sends.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.budget import conversation_usage_tokens
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine
from tests.dbsupport import test_database_url as _test_database_url

pytestmark = pytest.mark.anyio


def _test_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    from app.agent.session_store import DbSessionRepository

    return DbSessionRepository(_test_database_url())

TO = "whatsapp:+34600000000"
FROM_A = "whatsapp:+34600000001"
FROM_B = "whatsapp:+34600000002"


def make_message(sid: str, body: str, sender: str = FROM_A) -> dict[str, str]:
    return {"MessageSid": sid, "From": sender, "To": TO, "Body": body}


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
    yield factory
    await engine.dispose()


@pytest.fixture
def session_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    return _test_repository()


def _user_texts(invocation: dict[str, Any]) -> list[str]:
    """All user-role text of one recorded model invocation."""
    texts: list[str] = []
    for message in invocation["messages"]:
        if message.get("role") != "user":
            continue
        for block in message.get("content", []):
            if "text" in block:
                texts.append(block["text"])
    return texts


class TestConversationSurvivesProcessBoundaries:
    async def test_second_run_sees_the_first_turn(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """Two sequential pipeline runs on one conversation: the second
        invocation's input must contain the first turn — a different
        Celery process must not start from an empty conversation."""
        first = FakeModel(["primera respuesta."])
        second = FakeModel(["segunda respuesta."])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message("SMmem00000000000000000000000001", "me duele la rodilla al correr"),
            settings=make_settings(),
            session_factory=session_factory,
            model=first,
            twilio_client=twilio,
            session_repository=session_repository,
        )
        await process_inbound_message(
            make_message("SMmem00000000000000000000000002", "¿y si bajo la carga?"),
            settings=make_settings(),
            session_factory=session_factory,
            model=second,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        assert len(second.invocations) == 1
        user_texts = _user_texts(second.invocations[0])
        assert any("me duele la rodilla al correr" in text for text in user_texts), (
            "the second invocation must see the first turn (persisted memory)"
        )

    async def test_memory_survives_a_fresh_engine_and_repository(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The second run uses a NEW session factory/engine and a NEW
        repository instance on the same test database — the memory must
        come from the DATABASE, never from process memory."""
        await process_inbound_message(
            make_message("SMmem00000000000000000000000003", "hola, recuerda el 42"),
            settings=make_settings(),
            session_factory=session_factory,
            model=FakeModel(["recuerdado."]),
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        # A completely fresh engine + factory + repository: the shape a
        # different Celery worker process would have.
        fresh_engine = await create_test_engine()
        fresh_repository = _test_repository()
        try:
            fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
            second = FakeModel(["listo."])
            await process_inbound_message(
                make_message("SMmem00000000000000000000000004", "¿qué número te dije?"),
                settings=make_settings(),
                session_factory=fresh_factory,
                model=second,
                twilio_client=FakeTwilioClient(),
                session_repository=fresh_repository,
            )
            assert len(second.invocations) == 1
            user_texts = _user_texts(second.invocations[0])
            assert any("recuerda el 42" in text for text in user_texts), (
                "memory must be reloaded from the database in a fresh process"
            )
        finally:
            await fresh_engine.dispose()


class TestRollingSummary:
    async def test_summary_generated_once_and_reused(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """With window=4 verbatim turns and trigger=3: run 4 crosses the
        threshold and generates the summary ONCE (one extra model
        invocation); run 5 REUSES the stored summary — no regeneration —
        and the summary invocation is a real model call over the folded
        turns."""
        settings = make_settings(
            agent_memory_window_messages=4,
            agent_summary_trigger_messages=3,
        )
        bodies = [
            "mensaje uno",
            "mensaje dos",
            "mensaje tres",
            "mensaje cuatro",
            "mensaje cinco",
        ]
        models: list[FakeModel] = []
        for i, body in enumerate(bodies):
            # Runs 1-4: one turn each; run 4's script carries the summary
            # turn; run 5's script has NO spare turn — a summary
            # regeneration would raise "ran out of scripted turns".
            turns: list[str | dict[str, Any]] = ["respuesta."]
            if i == 3:
                turns.append("RESUMEN: el atleta hablo de sus cuatro mensajes.")
            model = FakeModel(turns)
            models.append(model)
            await process_inbound_message(
                make_message(f"SMmem0000000000000000000000001{i + 1}", body),
                settings=settings,
                session_factory=session_factory,
                model=model,
                twilio_client=FakeTwilioClient(),
                session_repository=session_repository,
            )

        # Runs 1-3: no summary (below the threshold): 1 invocation each.
        assert len(models[0].invocations) == 1
        assert len(models[1].invocations) == 1
        assert len(models[2].invocations) == 1
        # Run 4 crossed the threshold: the turn + exactly ONE summary
        # invocation, over the folded turns.
        assert len(models[3].invocations) == 2
        summary_invocation = models[3].invocations[1]
        summary_input = " ".join(
            block.get("text", "")
            for message in summary_invocation["messages"]
            for block in message.get("content", [])
        )
        assert "mensaje uno" in summary_input
        assert "mensaje dos" in summary_input

        # Run 5 REUSES the stored summary: exactly one invocation (the
        # turn) — no regeneration — and the model sees the summary text
        # plus the recent turns, not the folded ones verbatim.
        assert len(models[4].invocations) == 1
        run5_texts = _user_texts(models[4].invocations[0])
        assert any("RESUMEN" in text for text in run5_texts), (
            "the stored summary must be reused verbatim in the next run"
        )
        assert not any("mensaje uno" in text for text in run5_texts), (
            "folded turns must not stay verbatim beyond the window"
        )
        assert any("mensaje cuatro" in text for text in run5_texts), (
            "the last N turns must stay verbatim"
        )

    async def test_summary_cost_counts_against_the_conversation_budget(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The summary invocation's usage is persisted with the same
        provenance as any turn and seeds the conversation budget like any
        other turn's usage (fake model: 15 tokens per invocation)."""
        settings = make_settings(
            agent_memory_window_messages=2,
            agent_summary_trigger_messages=3,
        )
        for i, body in enumerate(["uno", "dos", "tres"]):
            turns: list[str | dict[str, Any]] = ["respuesta."]
            if i == 2:
                # Run 3 crosses window+trigger: turn + summary invocation.
                turns.append("RESUMEN corto.")
            await process_inbound_message(
                make_message(f"SMmem0000000000000000000000002{i + 1}", body),
                settings=settings,
                session_factory=session_factory,
                model=FakeModel(turns),
                twilio_client=FakeTwilioClient(),
                session_repository=session_repository,
            )

        # Runs 1-2: one turn each (2 x 15). Run 3: turn + summary
        # (2 x 15). The summary's 15 tokens MUST count against the
        # conversation budget like any other turn.
        async with session_factory() as session:
            total = await conversation_usage_tokens(session, sender=FROM_A)
        assert total == 4 * 15


class TestSenderIsolation:
    async def test_one_senders_history_never_leaks_into_another(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """Sender A talks first; sender B's run must never see A's turns."""
        await process_inbound_message(
            make_message("SMmem00000000000000000000000031", "secreto del atleta A",
                         sender=FROM_A),
            settings=make_settings(),
            session_factory=session_factory,
            model=FakeModel(["ok A."]),
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )
        model_b = FakeModel(["ok B."])
        await process_inbound_message(
            make_message("SMmem00000000000000000000000032", "hola soy B", sender=FROM_B),
            settings=make_settings(),
            session_factory=session_factory,
            model=model_b,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        texts = " ".join(_user_texts(model_b.invocations[0]))
        assert "secreto del atleta A" not in texts, (
            "one sender's history must never leak into another's conversation"
        )


class TestRetryStillDoesNothing:
    async def test_duplicate_message_sid_performs_zero_invocations_and_sends(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The message_sid idempotency anchor is untouched by memory: a
        Twilio retry hits the anchor before the agent — 0 invocations,
        0 sends — even though the conversation already has stored state."""
        sid = "SMmem00000000000000000000000041"
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(sid, "primera vez"),
            settings=make_settings(),
            session_factory=session_factory,
            model=FakeModel(["respuesta primera."]),
            twilio_client=twilio,
            session_repository=session_repository,
        )

        retry_model = FakeModel([])  # would raise if the loop invoked it
        await process_inbound_message(
            make_message(sid, "primera vez"),
            settings=make_settings(),
            session_factory=session_factory,
            model=retry_model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        assert len(retry_model.invocations) == 0
        assert len(twilio.messages.calls) == 1
        # And the audit trail still holds exactly one inbound row for the sid.
        async with session_factory() as session:
            inbound = (
                (
                    await session.execute(
                        select(MessageLogRow).where(
                            MessageLogRow.direction == "inbound",
                            MessageLogRow.message_sid == sid,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(inbound) == 1
