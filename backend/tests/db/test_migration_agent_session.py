"""Migration round-trip for the WA-8 agent-session tables.

Runs the real Alembic migration chain against the DEDICATED test database
(never the dev database, ``tests/dbsupport.py``) and proves:

- ``alembic upgrade head`` creates the four conversation-memory tables
  (``agent_session``, ``agent_session_agent``, ``agent_session_message``,
  ``agent_conversation_summary``) with the identity anchors the SDK's
  session seam relies on — a repeated (session, agent, message_id) row is
  rejected by the MIGRATED schema, not just the ORM;
- a conversation message row round-trips through the migrated schema;
- ``alembic downgrade -1`` (to ``c9e2a4f6b8d1``) removes the tables;
- re-upgrading makes them usable again.

Requires the compose Postgres; skips cleanly without it.
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.dbsupport import assert_is_test_database, create_test_engine
from tests.dbsupport import test_database_url as _test_database_url

pytestmark = pytest.mark.anyio

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "c9e2a4f6b8d1"

TABLES = (
    "agent_session",
    "agent_session_agent",
    "agent_session_message",
    "agent_conversation_summary",
)


def _alembic_config() -> Config:
    """Alembic config pinned to the ASSERTED test database."""
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", assert_is_test_database(_test_database_url()))
    return cfg


async def _alembic(action: Callable[[Config, str], None], cfg: Config, target: str) -> None:
    """Run an alembic command off the test's event loop."""
    import anyio

    await anyio.to_thread.run_sync(lambda: action(cfg, target))


@pytest.fixture
async def migrated_test_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    """Fresh test-DB schema driven by the real migration chain."""
    try:
        engine = await create_test_engine()
    except Exception:
        pytest.skip(
            "Postgres unreachable; migration tests require the compose "
            "Postgres: docker compose -f infra/docker-compose.yml --env-file .env up -d postgres"
        )

    assert_is_test_database(_test_database_url())
    # Neutralise env.py's fileConfig (same logger-pollution guard as the
    # other migration tests).
    import logging.config

    monkeypatch.setattr(logging.config, "fileConfig", lambda *a, **k: None)

    async with engine.begin() as conn:
        await conn.execute(text("drop table if exists alembic_version cascade"))
        from app.db.models import Base

        await conn.run_sync(Base.metadata.drop_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("select 1 from information_schema.tables where table_name = :table"),
                {"table": table},
            )
        ).fetchall()
    return bool(rows)


class TestAgentSessionMigrationRoundTrip:
    async def test_upgrade_creates_and_downgrade_removes_agent_session_tables(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        for table in TABLES:
            assert not await _table_exists(engine, table)

        await _alembic(command.upgrade, cfg, "head")
        for table in TABLES:
            assert await _table_exists(engine, table), table

        # A conversation message row round-trips through the migrated
        # schema (JSONB payload, sequential per (session, agent, message)).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into agent_session (session_id, session_type, data, "
                    "created_at, updated_at) values "
                    "('whatsapp:+34600000001', 'AGENT', '{}', now(), now())"
                )
            )
            await conn.execute(
                text(
                    "insert into agent_session_message (session_id, agent_id, "
                    "message_id, role, data, created_at) values "
                    "('whatsapp:+34600000001', 'default', 0, 'user', "
                    "'{\"role\": \"user\", \"content\": [{\"text\": \"hola\"}]}', now())"
                )
            )
        async with engine.connect() as conn:
            role = (
                await conn.execute(
                    text(
                        "select role from agent_session_message "
                        "where session_id = 'whatsapp:+34600000001'"
                    )
                )
            ).scalar_one()
        assert role == "user"

        # The migrated unique constraint anchors the message identity: a
        # repeated (session_id, agent_id, message_id) cannot insert twice.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "insert into agent_session_message (session_id, agent_id, "
                        "message_id, role, data, created_at) values "
                        "('whatsapp:+34600000001', 'default', 0, 'assistant', '{}', now())"
                    )
                )

        # Downgrade one step removes the tables again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        for table in TABLES:
            assert not await _table_exists(engine, table)

        # Re-upgrading makes them usable again.
        await _alembic(command.upgrade, cfg, "head")
        for table in TABLES:
            assert await _table_exists(engine, table), table
