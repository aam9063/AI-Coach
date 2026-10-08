"""WA-3 RED tests: the Celery task processes an inbound message end-to-end
against the fake LLM model (§6, §9.1).

The full pipeline, exercised with ZERO network and ZERO credentials:

  inbound Twilio payload
    -> idempotency anchor (MessageSid unique in ``message_log``)
    -> inbound row persisted (trace id assigned, agent_version stamped)
    -> Strands agent (the SDK's loop) with an EXPLICIT tool list, the
       system-prompt stub and the FAKE model runs
    -> tool-call rows + outbound row persisted
    -> reply sent through the Twilio REST client (faked here)

Pinned contract:

- ``handle_inbound_message`` is the injectable seam the Celery task
  calls; every dependency (settings, session factory, model, Twilio
  client) can be injected, and the default wiring derives each from
  configuration (``get_settings`` / ``app.db.session`` /
  ``app.agent.model_factory.create_model`` / ``twilio.rest.Client``);
- a Twilio retry of the same MessageSid is a NO-OP: no second agent run,
  no second Twilio send, no new rows;
- the agent's reply text is exactly what the Twilio client is asked to
  send and what the outbound row records;
- every row of one processing turn shares one trace id.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.fake_model import FakeModel
from app.agent.pipeline import AGENT_VERSION, handle_inbound_message, process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMtest00000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"
BODY = "¿cómo estoy de forma?"


def make_message(**overrides: str) -> dict[str, str]:
    """A validated Twilio inbound payload (what the webhook hands over)."""
    return {"MessageSid": SID, "From": FROM, "To": TO, "Body": BODY, **overrides}


def make_task_settings() -> Settings:
    """Settings for pipeline tests: fake provider, no credentials at all."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="",
        twilio_auth_token="",
    )


class FakeTwilioMessages:
    """Captures what the pipeline asks the Twilio REST API to send."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, **kwargs: str) -> Any:
        self.calls.append(kwargs)
        return {"sid": "SMoutbound1"}


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeTwilioMessages()


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[Any], None]:
    """Session factory on the dedicated test database."""
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


class TestEndToEndPipeline:
    async def test_reply_sent_and_all_rows_written(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        model = FakeModel(
            [
                {"name": "get_load_status", "input": {"date_range": "7d"}},
                "Tu CTL es 45.0 y tu TSB -2.1.",
            ]
        )
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(),
            settings=make_task_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )

        # 1. Reply sent through the Twilio REST client, exactly once, with
        #    the agent's text reversed-addressed (From<->To).
        assert twilio.messages.calls == [
            {"to": FROM, "from_": TO, "body": "Tu CTL es 45.0 y tu TSB -2.1."}
        ]

        # 2. Inbound row carries the MessageSid (idempotency anchor) and body.
        async with session_factory() as session:
            rows = (await session.execute(
                select(MessageLogRow).order_by(MessageLogRow.id)
            )).scalars().all()

        inbound = [r for r in rows if r.direction == "inbound"]
        assert len(inbound) == 1
        assert inbound[0].message_sid == SID
        assert inbound[0].body == BODY

        # 3. Tool call recorded with name and inputs/outputs.
        tool_rows = [r for r in rows if r.direction == "tool_call"]
        assert len(tool_rows) == 1
        assert tool_rows[0].tool_name == "get_load_status"
        assert tool_rows[0].payload is not None
        assert tool_rows[0].payload["input"] == {"date_range": "7d"}

        # 4. Outbound row records the sent reply.
        outbound = [r for r in rows if r.direction == "outbound"]
        assert len(outbound) == 1
        assert outbound[0].body == "Tu CTL es 45.0 y tu TSB -2.1."

        # 5. One trace id shared by the whole turn; provenance stamped.
        trace_ids = {r.trace_id for r in rows}
        assert len(trace_ids) == 1
        assert all(r.agent_version == AGENT_VERSION for r in rows)

    async def test_retry_of_same_message_sid_is_a_no_op(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        model = FakeModel(["primera respuesta"])
        twilio = FakeTwilioClient()

        for _ in range(2):
            await process_inbound_message(
                make_message(),
                settings=make_task_settings(),
                session_factory=session_factory,
                model=model,
                twilio_client=twilio,
            )

        # One Twilio send, one set of rows, ONE agent invocation.
        assert len(twilio.messages.calls) == 1
        async with session_factory() as session:
            count = (await session.execute(
                select(func.count()).select_from(MessageLogRow)
            )).scalar_one()
        assert count == 2  # inbound + outbound; the retry added nothing
        assert len(model.invocations) == 1

    async def test_rows_without_tool_calls_when_agent_answers_directly(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        model = FakeModel(["Respuesta directa."])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            make_message(),
            settings=make_task_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )

        async with session_factory() as session:
            rows = (await session.execute(select(MessageLogRow))).scalars().all()
        assert [r.direction for r in rows] == ["inbound", "outbound"]
        assert twilio.messages.calls[0]["body"] == "Respuesta directa."


class TestHandleInboundMessageSeam:
    def test_injections_pass_through_to_the_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``handle_inbound_message`` forwards every injected dependency."""
        received: dict[str, Any] = {}

        async def fake_pipeline(message: dict[str, str], **kwargs: Any) -> None:
            received["message"] = message
            received.update(kwargs)

        monkeypatch.setattr("app.agent.pipeline.process_inbound_message", fake_pipeline)

        settings = make_task_settings()
        model = FakeModel(["x"])
        twilio = FakeTwilioClient()
        session_factory = object()  # sentinel: forwarded untouched

        handle_inbound_message(
            make_message(),
            settings=settings,
            session_factory=session_factory,  # type: ignore[arg-type]
            model=model,
            twilio_client=twilio,
        )

        assert received["message"] == make_message()
        assert received["settings"] is settings
        assert received["session_factory"] is session_factory
        assert received["model"] is model
        assert received["twilio_client"] is twilio

    def test_default_wiring_is_derived_from_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No injections: settings, sessions, model and Twilio client are
        derived from configuration (the production path)."""
        captured: dict[str, Any] = {}

        async def fake_pipeline(message: dict[str, str], **kwargs: Any) -> None:
            captured["message"] = message
            captured.update(kwargs)

        monkeypatch.setattr("app.agent.pipeline.process_inbound_message", fake_pipeline)

        import app.agent.pipeline as pipeline_module
        from app.core.settings import get_settings

        def fake_create_model(settings: Settings) -> object:
            captured["model_built_from"] = settings
            return object()

        monkeypatch.setattr(pipeline_module, "create_model", fake_create_model)
        handle_inbound_message(make_message())

        assert captured["message"] == make_message()
        assert captured["settings"] is get_settings()
        assert captured["session_factory"] is not None
        assert captured["model_built_from"] is get_settings()
        assert captured["twilio_client"] is not None
        # The tool list handed to the agent is an EXPLICIT list of this
        # project's tools only (never directory-loaded, never vended).
        assert isinstance(pipeline_module.tool_list(), list)


class TestCeleryTaskDelegation:
    def test_celery_task_calls_handle_inbound_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The eager Celery task hands the validated payload to the seam."""
        calls: list[dict[str, Any]] = []

        def _record(message: dict[str, Any], **kwargs: Any) -> None:
            calls.append(message)

        from app.scheduler import whatsapp_tasks
        from app.scheduler.celery_app import celery_app

        monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
        monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
        monkeypatch.setattr(whatsapp_tasks, "handle_inbound_message", _record)

        whatsapp_tasks.process_whatsapp_message.run(make_message())

        assert calls == [make_message()]
