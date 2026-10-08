"""Migration round-trip for the WA-6 ``subjective_log`` table.

Runs the real Alembic migration chain against the DEDICATED test database
(never the dev database, ``tests/dbsupport.py``) and proves:

- ``alembic upgrade head`` creates ``subjective_log`` with the unique
  ``(athlete_id, date)`` idempotency anchor (a second row for the same
  day is rejected by the MIGRATED schema, not just the ORM);
- rows round-trip through the migrated schema;
- ``alembic downgrade -1`` (to ``d1b9c8e2a4f6``) removes the table;
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
PREVIOUS_HEAD = "d1b9c8e2a4f6"


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


class TestSubjectiveLogMigrationRoundTrip:
    async def test_upgrade_creates_and_downgrade_removes_subjective_log(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        assert not await _table_exists(engine, "subjective_log")

        await _alembic(command.upgrade, cfg, "head")
        assert await _table_exists(engine, "subjective_log")

        # Rows round-trip through the migrated schema: the owner's own
        # report for one day (scales documented on the tool/table).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into subjective_log (athlete_id, date, rpe, "
                    "fatigue, soreness, notes, recorded_at) values "
                    "(1, '2026-10-08', 7.5, 8, 3, 'día duro', now())"
                )
            )
        async with engine.connect() as conn:
            fatigue = (
                await conn.execute(
                    text("select fatigue from subjective_log where athlete_id = 1")
                )
            ).scalar_one()
        assert fatigue == 8

        # The migrated unique constraint is the idempotency anchor:
        # logging twice the same day cannot duplicate the row.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "insert into subjective_log (athlete_id, date, rpe, "
                        "fatigue, soreness, notes, recorded_at) values "
                        "(1, '2026-10-08', 6.0, 5, 2, 'segundo reporte', now())"
                    )
                )

        # Downgrade one step removes the table again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        assert not await _table_exists(engine, "subjective_log")

        # Re-upgrading makes it usable again.
        await _alembic(command.upgrade, cfg, "head")
        assert await _table_exists(engine, "subjective_log")
