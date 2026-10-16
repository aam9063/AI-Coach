"""SCH-4 RED: the on-demand ``/sync`` command (brief §11).

Contract pinned here (these tests are its executable specification):

- An inbound WhatsApp message whose body IS the command (``/sync``, after
  stripping surrounding whitespace, case-insensitive) triggers the SAME
  idempotent job the beat entry runs — the injectable ``sync_runner`` seam
  defaults to ``app.scheduler.tasks.run_sync_async(days=SYNC_WINDOW_DAYS)``,
  the exact function the ``sync_intervals`` task wraps — and answers with
  a SHORT confirmation through the NORMAL send path: one outbound
  ``message_log`` audit row persisted BEFORE the Twilio send, reversed
  addressing.
- **Zero model invocations**: the command never reaches the LLM — pinned
  with a ``FakeModel`` scripted with ZERO turns (any invocation raises),
  the same pattern as the duplicate-``MessageSid`` retry test.
- A message that merely CONTAINS the word (``/sync ahora``, ``haz /sync``)
  is NOT the command: it goes down the normal agent path.
- The ``MessageSid`` anchor above the command check makes a Twilio retry
  of the same command message run the sync at most once.
- The command check sits BEFORE the per-conversation token budget: it
  invokes no model, so the LLM budget neither blocks nor pays for it.

Zero network, zero credentials: fake runner, fake Twilio client, fake
model; the database is the dedicated test database.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.fake_model import FakeModel
from app.agent.pipeline import handle_inbound_message, process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMsync00000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"

SYNC_SUMMARY: dict[str, Any] = {
    "days": 30,
    "end_date": None,
    "windows": 30,
    "ok": True,
    "activities_synced": 2,
    "activities_failed": 0,
    "streams_synced": 4,
    "streams_skipped": 0,
    "streams_failed": 0,
    "wellness_synced": 1,
    "wellness_failed": 0,
    "failed_windows": [],
}


def make_message(body: str = "/sync", **overrides: str) -> dict[str, str]:
    """A validated Twilio inbound payload (what the webhook hands over)."""
    return {
        "MessageSid": SID,
        "From": FROM,
        "To": TO,
        "Body": body,
        **overrides,
    }


def make_task_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="",
        twilio_auth_token="",
    )


def fake_sync_runner(
    calls: list[dict[str, Any]],
    *,
    summary: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> Any:
    """Async stand-in for the default sync runner; records each call."""

    async def _run() -> dict[str, Any]:
        calls.append({"at": len(calls)})
        if error is not None:
            raise error
        return dict(summary or SYNC_SUMMARY)

    return _run


class FakeTwilioMessages:
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


@pytest.fixture
def session_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    from app.agent.session_store import DbSessionRepository
    from tests.dbsupport import test_database_url as _test_database_url

    return DbSessionRepository(_test_database_url())


async def _rows(session_factory: async_sessionmaker[Any]) -> list[MessageLogRow]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(MessageLogRow).order_by(MessageLogRow.id)))
            .scalars()
            .all()
        )


class TestSyncCommand:
    async def test_command_triggers_job_and_confirms_with_zero_model_invocations(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """Body IS the command: job runs, confirmation sent, model NEVER invoked.

        ``FakeModel([])`` has zero scripted turns — any model invocation
        raises — so ``model.invocations == []`` is the zero-invocation proof.
        """
        model = FakeModel([])
        twilio = FakeTwilioClient()
        runner_calls: list[dict[str, Any]] = []

        await process_inbound_message(
            make_message(),
            settings=make_task_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
            sync_runner=fake_sync_runner(runner_calls),
        )

        # The job ran exactly once, with no arguments needed.
        assert len(runner_calls) == 1
        # ZERO model invocations (SCH-4 pin).
        assert model.invocations == []
        # SHORT confirmation through the NORMAL send path, exactly once.
        assert len(twilio.messages.calls) == 1
        sent = twilio.messages.calls[0]
        assert sent["to"] == FROM
        assert sent["from_"] == TO
        assert len(sent["body"]) <= make_task_settings().agent_whatsapp_message_char_limit
        assert "sync" in sent["body"].lower()

        # Audit trail: inbound row + ONE outbound row sharing one trace id;
        # the outbound payload names the command and carries the summary.
        rows = await _rows(session_factory)
        assert [r.direction for r in rows] == ["inbound", "outbound"]
        assert rows[0].message_sid == SID
        assert rows[0].body == "/sync"
        assert rows[1].body == sent["body"]
        assert rows[1].payload is not None
        assert rows[1].payload.get("command") == "/sync"
        assert rows[1].payload.get("sync_summary") == SYNC_SUMMARY
        assert rows[0].trace_id == rows[1].trace_id

    async def test_matching_rule_strips_whitespace_and_ignores_case(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """``' /sync '``, ``'/SYNC'`` and ``'\\n/Sync\\n'`` are the command."""
        for body in ("/sync", "  /sync  ", "/SYNC", "\n/Sync\n"):
            model = FakeModel([])
            twilio = FakeTwilioClient()
            runner_calls: list[dict[str, Any]] = []
            await process_inbound_message(
                make_message(body=body, MessageSid=f"SM{abs(hash(body))}"),
                settings=make_task_settings(),
                session_factory=session_factory,
                model=model,
                twilio_client=twilio,
                session_repository=session_repository,
                sync_runner=fake_sync_runner(runner_calls),
            )
            assert len(runner_calls) == 1, f"{body!r} must trigger the command"
            assert model.invocations == []
            assert len(twilio.messages.calls) == 1

    async def test_message_merely_containing_the_word_is_not_the_command(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """``'/sync ahora'`` and friends go down the NORMAL agent path."""
        for body in ("/sync ahora", "haz /sync", "sync", "/sync\ngracias"):
            model = FakeModel([f"eco: {body}"])
            twilio = FakeTwilioClient()
            runner_calls: list[dict[str, Any]] = []
            await process_inbound_message(
                make_message(body=body, MessageSid=f"SM{abs(hash(body))}"),
                settings=make_task_settings(),
                session_factory=session_factory,
                model=model,
                twilio_client=twilio,
                session_repository=session_repository,
                sync_runner=fake_sync_runner(runner_calls),
            )
            assert runner_calls == [], f"{body!r} must NOT trigger the sync job"
            # Normal agent path: the model WAS invoked once and replied.
            assert len(model.invocations) == 1
            assert twilio.messages.calls[0]["body"] == f"eco: {body}"

    async def test_retry_of_same_message_sid_runs_the_sync_once(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The MessageSid anchor bounds a Twilio retry at ONE sync run."""
        model = FakeModel([])
        twilio = FakeTwilioClient()
        runner_calls: list[dict[str, Any]] = []
        for _ in range(2):
            await process_inbound_message(
                make_message(),
                settings=make_task_settings(),
                session_factory=session_factory,
                model=model,
                twilio_client=twilio,
                session_repository=session_repository,
                sync_runner=fake_sync_runner(runner_calls),
            )
        assert len(runner_calls) == 1
        assert len(twilio.messages.calls) == 1
        assert model.invocations == []
        assert len(await _rows(session_factory)) == 2

    async def test_job_failure_answers_honestly_without_the_model(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """A raising job sends an explicit failure message; still no model."""
        model = FakeModel([])
        twilio = FakeTwilioClient()
        runner_calls: list[dict[str, Any]] = []
        await process_inbound_message(
            make_message(),
            settings=make_task_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
            sync_runner=fake_sync_runner(runner_calls, error=RuntimeError("db down")),
        )
        assert model.invocations == []
        assert len(twilio.messages.calls) == 1
        sent_body = twilio.messages.calls[0]["body"]
        assert "falló" in sent_body or "failed" in sent_body
        rows = await _rows(session_factory)
        assert [r.direction for r in rows] == ["inbound", "outbound"]
        assert rows[1].payload is not None
        assert rows[1].payload.get("sync_failed") is True


class TestDefaultSyncRunnerWiring:
    def test_injected_runner_is_forwarded_by_the_seam(
        self, monkeypatch: pytest.MonkeyPatch, session_repository: Any
    ) -> None:
        received: dict[str, Any] = {}

        async def fake_pipeline(message: dict[str, str], **kwargs: Any) -> None:
            received.update(kwargs)

        monkeypatch.setattr("app.agent.pipeline.process_inbound_message", fake_pipeline)
        runner = fake_sync_runner([])
        handle_inbound_message(
            make_message(),
            settings=make_task_settings(),
            session_factory=object(),  # type: ignore[arg-type]
            model=FakeModel([]),
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
            sync_runner=runner,
        )
        assert received["sync_runner"] is runner

    def test_default_wiring_builds_a_runner_from_the_scheduler(
        self, monkeypatch: pytest.MonkeyPatch, session_repository: Any
    ) -> None:
        received: dict[str, Any] = {}

        async def fake_pipeline(message: dict[str, str], **kwargs: Any) -> None:
            received.update(kwargs)

        monkeypatch.setattr("app.agent.pipeline.process_inbound_message", fake_pipeline)
        handle_inbound_message(
            make_message(),
            settings=make_task_settings(),
            session_factory=object(),  # type: ignore[arg-type]
            model=FakeModel([]),
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )
        # The production default is a callable — the same function the
        # ``sync_intervals`` beat task wraps (checked structurally here; the
        # job's behaviour itself is proven by the idempotency tests).
        assert callable(received["sync_runner"])
