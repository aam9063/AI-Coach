"""Migration round-trip for the owner-entered RPE column (ODD LOAD-12).

Runs the real Alembic migration chain against the DEDICATED test database
(``<database>_test``, never the configured dev database — the test
monkeypatches ``app.core.settings.get_settings`` so ``env.py`` resolves the
test URL) and proves:

- ``alembic upgrade head`` adds the nullable ``activity.rpe`` column;
- a stored RPE round-trips through the migrated schema;
- ``alembic downgrade`` to the previous head (``a8c3e5f70b12``) removes it;
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
PREVIOUS_HEAD = "a8c3e5f70b12"


def _alembic_config() -> Config:
    """Alembic config pinned to the ASSERTED test database.

    ``env.py`` resolves this explicit option before the cached settings, so
    the round-trip cannot reach the developer database even if
    ``get_settings()`` was already cached by an earlier test (that cache is
    how a migration once landed on the dev database).
    """
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
    """Run an alembic command off the test's event loop (env.py calls
    ``asyncio.run`` itself, which cannot run inside a live loop)."""
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

    url = _test_database_url()
    assert_is_test_database(url)
    # The URL is pinned through Alembic's own config option instead of
    # monkeypatching get_settings: env.py is re-executed per command and now
    # prefers that option, which keeps this test independent of the settings
    # cache and unable to touch a non-test database.
    # env.py runs ``fileConfig(alembic.ini)``, whose default
    # ``disable_existing_loggers`` would silently kill every logger created
    # before it (e.g. ``app.ingest.backfill``) for the rest of the pytest
    # process — a cross-test pollution observed against the ING-7 logging
    # tests. Alembic's logging setup is irrelevant to this test, so it is
    # neutralised here.
    import logging.config

    monkeypatch.setattr(logging.config, "fileConfig", lambda *a, **k: None)

    # Start from a clean, unmigrated database (this database is dedicated
    # to tests; the dev database is refused by dbsupport by design).
    async with engine.begin() as conn:
        await conn.execute(text("drop table if exists alembic_version cascade"))
        await conn.run_sync(lambda sync_conn: None)
        from app.db.models import Base

        await conn.run_sync(Base.metadata.drop_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _rpe_column_exists(engine) -> bool:  # type: ignore[no-untyped-def]
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_name = 'activity' and column_name = 'rpe'"
                )
            )
        ).fetchall()
    return bool(rows)


class TestRpeMigrationRoundTrip:
    async def test_upgrade_adds_and_downgrade_removes_rpe(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        assert not await _rpe_column_exists(engine)

        await _alembic(command.upgrade, cfg, "head")
        assert await _rpe_column_exists(engine)

        # The migrated column round-trips a stored value.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into activity (source, source_id, type, name, "
                    "start_time, rpe) values ('intervals', 'i-mig-1', "
                    "'WeightTraining', 'Gym', now(), 7)"
                )
            )
        async with engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("select rpe from activity where source_id = 'i-mig-1'")
                )
            ).scalar_one()
        assert stored == 7

        # Downgrade to the previous head removes the column again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        assert not await _rpe_column_exists(engine)

        # Re-upgrading makes the column usable again (empty: no data loss
        # beyond the documented drop of the downgraded column).
        await _alembic(command.upgrade, cfg, "head")
        assert await _rpe_column_exists(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into activity (source, source_id, type, name, "
                    "start_time, rpe) values ('intervals', 'i-mig-2', "
                    "'Workout', 'Gym', now(), 10)"
                )
            )
        async with engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("select rpe from activity where source_id = 'i-mig-2'")
                )
            ).scalar_one()
        assert stored == 10
