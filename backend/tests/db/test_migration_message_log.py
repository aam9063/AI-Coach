"""Migration round-trip for the WA-3 ``message_log`` table.


Runs the real Alembic migration chain against the DEDICATED test database
(never the dev database, ``tests/dbsupport.py``) and proves:

- ``alembic upgrade head`` creates ``message_log`` with the unique
  ``message_sid`` idempotency anchor (a second row with the same
  MessageSid is rejected by the MIGRATED schema, not just the ORM);
- rows round-trip through the migrated schema;
- ``alembic downgrade -1`` (to ``f0a1b2c3d4e5``) removes the table;
- re-upgrading makes it usable again.

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
PREVIOUS_HEAD = "f0a1b2c3d4e5"


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


class TestMessageLogMigrationRoundTrip:
    async def test_upgrade_creates_and_downgrade_removes_message_log(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        assert not await _table_exists(engine, "message_log")

        await _alembic(command.upgrade, cfg, "head")
        assert await _table_exists(engine, "message_log")

        # Rows round-trip through the migrated schema, including the JSONB
        # tool payload and the nullable message_sid of non-inbound rows.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into message_log (direction, message_sid, body, "
                    "trace_id, agent_version, created_at) values "
                    "('inbound', 'SMabc123', 'hola', 't1', '0.1.0', now())"
                )
            )
            await conn.execute(
                text(
                    "insert into message_log (direction, body, trace_id, "
                    "agent_version, created_at) values "
                    "('outbound', 'Tu CTL es 45.', 't1', '0.1.0', now())"
                )
            )
            await conn.execute(
                text(
                    "insert into message_log (direction, tool_name, payload, "
                    "trace_id, agent_version, created_at) values "
                    "('tool_call', 'get_load_status', "
                    "'{\"input\": {\"date_range\": \"7d\"}}', 't1', '0.1.0', now())"
                )
            )
        async with engine.connect() as conn:
            payload_input = (
                await conn.execute(
                    text("select payload->>'input' from message_log where tool_name is not null")
                )
            ).scalar_one()

        assert payload_input == '{"date_range": "7d"}'

        # The migrated unique constraint is the idempotency anchor: a
        # Twilio retry with the same MessageSid cannot insert twice.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "insert into message_log (direction, message_sid, body, "
                        "trace_id, agent_version, created_at) values "
                        "('inbound', 'SMabc123', 'hola (retry)', 't2', '0.1.0', now())"
                    )
                )

        # Downgrade one step removes the table again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        assert not await _table_exists(engine, "message_log")

        # Re-upgrading makes it usable again.
        await _alembic(command.upgrade, cfg, "head")
        assert await _table_exists(engine, "message_log")
