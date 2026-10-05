"""Migration round-trip for the ZON-10 athlete tables.

Runs the real Alembic migration chain against the DEDICATED test database
(``<database>_test``, pinned through ``env.py:resolve_database_url()`` via
Alembic's own ``sqlalchemy.url`` option — never the configured dev
database, see ``tests/dbsupport.py``) and proves:

- ``alembic upgrade head`` creates ``athlete_profile`` and
  ``athlete_threshold_history``;
- rows round-trip through the migrated schema;
- ``alembic downgrade -1`` (to ``a8c3e5f70b12``) removes both tables;
- re-upgrading makes them usable again.

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
PREVIOUS_HEAD = "a8c3e5f70b12"
NEW_TABLES = ("athlete_profile", "athlete_threshold_history")


def _alembic_config() -> Config:
    """Alembic config pinned to the ASSERTED test database."""
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(BACKEND_ROOT / "app" / "db" / "migrations")
    )
    cfg.set_main_option(
        "sqlalchemy.url", assert_is_test_database(_test_database_url())
    )
    return cfg


async def _alembic(
    action: Callable[[Config, str], None], cfg: Config, target: str
) -> None:
    """Run an alembic command off the test's event loop."""
    import anyio

    await anyio.to_thread.run_sync(lambda: action(cfg, target))


@pytest.fixture
async def migrated_test_db(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """Fresh test-DB schema driven by the real migration chain."""
    try:
        engine = await create_test_engine()
    except Exception:
        pytest.skip(
            "Postgres unreachable; migration tests require the compose "
            "Postgres: docker compose -f infra/docker-compose.yml "
            "--env-file .env up -d postgres"
        )

    assert_is_test_database(_test_database_url())
    # Neutralise env.py's fileConfig (same logger-pollution guard as
    # tests/db/test_migration_rpe.py).
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
                text(
                    "select 1 from information_schema.tables "
                    "where table_name = :table"
                ),
                {"table": table},
            )
        ).fetchall()
    return bool(rows)


class TestAthleteTablesMigrationRoundTrip:
    async def test_upgrade_creates_and_downgrade_removes_athlete_tables(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        for table in NEW_TABLES:
            assert not await _table_exists(engine, table)

        await _alembic(command.upgrade, cfg, "head")
        for table in NEW_TABLES:
            assert await _table_exists(engine, table)

        # The migrated schema round-trips stored rows.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into athlete_profile (athlete_id, "
                    "intervals_athlete_id, ftp_watts, ftp_source, "
                    "engine_version, updated_at) values "
                    "(1, 'i555003', 180.0, 'manual', '0.1.0', now())"
                )
            )
            await conn.execute(
                text(
                    "insert into athlete_threshold_history (athlete_id, "
                    "metric, decision, prior_value, new_value, "
                    "proposed_value, evidence, confirmed_by, "
                    "engine_version, recorded_at) values "
                    "(1, 'ftp_watts', 'accepted', 180.0, 190.0, 190.0, "
                    "'test evidence', 'owner_whatsapp', '0.1.0', now())"
                )
            )
        async with engine.connect() as conn:
            ftp = (
                await conn.execute(
                    text("select ftp_watts from athlete_profile where athlete_id = 1")
                )
            ).scalar_one()
            evidence = (
                await conn.execute(
                    text(
                        "select evidence from athlete_threshold_history "
                        "where athlete_id = 1"
                    )
                )
            ).scalar_one()
        assert ftp == 180.0
        assert evidence == "test evidence"

        # Downgrade one step removes the ZON-10 tables again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        for table in NEW_TABLES:
            assert not await _table_exists(engine, table)

        # Re-upgrading makes them usable again.
        await _alembic(command.upgrade, cfg, "head")
        for table in NEW_TABLES:
            assert await _table_exists(engine, table)
