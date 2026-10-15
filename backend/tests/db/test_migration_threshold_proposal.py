"""Migration round-trip for the WA-6 ``threshold_proposal`` table.

Runs the real Alembic migration chain against the DEDICATED test database
(never the dev database, ``tests/dbsupport.py``) and proves:

- ``alembic upgrade head`` creates ``threshold_proposal`` with the
  columns the tool contract stores (sport, proposed_value, evidence,
  created_at, pending status);
- a new proposal persists with its DEFAULT ``pending`` status through
  the MIGRATED schema;
- ``alembic downgrade -1`` removes the table;
- re-upgrading makes it usable again.

Requires the compose Postgres; skips cleanly without it.
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.dbsupport import assert_is_test_database, create_test_engine
from tests.dbsupport import test_database_url as _test_database_url

pytestmark = pytest.mark.anyio

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "d2c4e6f8a0b2"


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


class TestThresholdProposalMigrationRoundTrip:
    async def test_upgrade_creates_and_downgrade_removes_threshold_proposal(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        assert not await _table_exists(engine, "threshold_proposal")

        await _alembic(command.upgrade, cfg, "head")
        assert await _table_exists(engine, "threshold_proposal")

        # A proposal persists through the migrated schema; the status
        # DEFAULT is 'pending' — the tool never has to remember to set it.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into threshold_proposal (athlete_id, sport, metric, "
                    "proposed_value, unit, evidence, engine_version) values "
                    "(1, 'ride', 'bike_power', 250.0, 'watts', "
                    "'best 20-min effort 250 W on 2026-10-01', 'test-engine-1.0')"
                )
            )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "select sport, proposed_value, evidence, status, "
                        "created_at from threshold_proposal where athlete_id = 1"
                    )
                )
            ).fetchone()
        assert row is not None
        assert row.sport == "ride"
        assert row.proposed_value == 250.0
        assert row.evidence == "best 20-min effort 250 W on 2026-10-01"
        assert row.status == "pending"
        assert row.created_at is not None

        # Downgrade one step removes the table again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        assert not await _table_exists(engine, "threshold_proposal")

        # Re-upgrading makes it usable again.
        await _alembic(command.upgrade, cfg, "head")
        assert await _table_exists(engine, "threshold_proposal")
