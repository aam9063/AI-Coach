"""Two consecutive turns in one worker process must BOTH work.

Regression for a defect measured in production on 2026-10-21. Caching the engine
per worker process made the first WhatsApp turn succeed and the second die with
``cannot rollback; the transaction is in error state``, losing the athlete's
message. Each Celery task runs its own ``asyncio.run``, and asyncpg connections
belong to the loop that created them, so a pool cached from the previous task is
unusable by the next one.

Each turn here runs through ``asyncio.to_thread``: the sync seam calls
``asyncio.run`` itself, so it needs a thread of its own, which is also exactly how
the Celery worker invokes it. Only the model and the Twilio client are injected,
so the DEFAULT engine path runs as production runs it.

Only the tool registry stays process-level, and an autouse fixture in
``tests/conftest.py`` resets it so a leaking test cannot reach the next one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.fake_model import FakeModel
from app.agent.pipeline import handle_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import test_database_url as dedicated_test_database_url

SENDER = "whatsapp:+10000000001"
COACH = "whatsapp:+10000000000"


class _RecordingTwilio:
    """The minimum Twilio surface the pipeline touches, recording sends."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        self.bodies: list[str] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.bodies.append(str(kwargs.get("body", "")))

        class _Message:
            sid = "SMoutbound"

        return _Message()


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=dedicated_test_database_url(),
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="ACtest",
        twilio_auth_token="test-token",
        twilio_whatsapp_from=COACH,
        twilio_whatsapp_allowlist=SENDER.removeprefix("whatsapp:"),
    )


@pytest.mark.anyio
async def test_two_consecutive_turns_in_one_process_both_persist(
    db_engine: Any,
) -> None:
    settings = _settings()

    for index in (1, 2):
        await asyncio.to_thread(
            handle_inbound_message,
            {
                "MessageSid": f"SMtwoTurns{index}",
                "From": SENDER,
                "To": COACH,
                "Body": "hola",
            },
            settings=settings,
            model=FakeModel(["respuesta"]),
            twilio_client=_RecordingTwilio(),
        )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        rows = (await session.execute(select(MessageLogRow))).scalars().all()
    sids = {row.message_sid for row in rows if row.direction == "inbound"}

    assert "SMtwoTurns1" in sids, "the first turn did not persist"
    assert "SMtwoTurns2" in sids, (
        "the second turn in the same process did not persist -- a per-process "
        "pool is bound to a dead event loop"
    )
