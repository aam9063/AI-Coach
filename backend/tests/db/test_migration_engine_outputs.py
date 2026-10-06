"""Migration round-trip for the RID-10 engine-output tables.

Runs the real Alembic migration chain against the DEDICATED test database
(``<database>_test``, pinned through ``env.py:resolve_database_url()`` via
Alembic's own ``sqlalchemy.url`` option — never the configured dev
database, see ``tests/dbsupport.py``) and proves:

- ``alembic upgrade head`` creates ``readiness_snapshot``,
  ``weekly_intensity`` and ``session_durability``;
- rows round-trip through the migrated schema (including the JSONB signal
  payload and the nullable ``not_steady`` durability columns);
- ``alembic downgrade -1`` (to ``c4d5e6f7a8b9``) removes all three tables;
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
PREVIOUS_HEAD = "c4d5e6f7a8b9"
NEW_TABLES = ("readiness_snapshot", "weekly_intensity", "session_durability")


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
    # tests/db/test_migration_athlete_profile.py).
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


class TestEngineOutputsMigrationRoundTrip:
    async def test_upgrade_creates_and_downgrade_removes_output_tables(
        self, migrated_test_db: AsyncEngine
    ) -> None:
        engine = migrated_test_db
        cfg = _alembic_config()

        for table in NEW_TABLES:
            assert not await _table_exists(engine, table)

        await _alembic(command.upgrade, cfg, "head")
        for table in NEW_TABLES:
            assert await _table_exists(engine, table)

        # The migrated schema round-trips stored rows: a readiness snapshot
        # with its JSONB signal payload, a weekly-intensity row with a NULL
        # percentages column (the no_data representation), and a
        # session_durability row with the not_steady NULL metrics.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into activity (source, source_id, type, name, "
                    "start_time) values ('intervals', 'i1', 'Ride', 'r', "
                    "now())"
                )
            )
            await conn.execute(
                text(
                    "insert into readiness_snapshot (athlete_id, date, "
                    "signals, adverse_signal_keys, agreement_count, "
                    "suggest_reduce_intensity, suggestion, reasons, tsb, "
                    "tsb_very_negative_below, subjective_fatigue_reported, "
                    "acwr, engine_version, computed_at) values "
                    "(1, '2026-08-31', "
                    "'[{\"key\": \"hrv_ln_rmssd\", \"status\": "
                    "\"insufficient_data\"}]', '[]', 0, false, null, '[]', "
                    "1.0, -10.0, null, null, '0.1.0', now())"
                )
            )
            await conn.execute(
                text(
                    "insert into weekly_intensity (athlete_id, iso_year, "
                    "iso_week, week_start, sport, status, z1_seconds, "
                    "z2_seconds, z3_seconds, total_seconds, percentages, "
                    "engine_version, computed_at) values "
                    "(1, 2026, 36, '2026-08-31', "
                    "'bike', 'no_data', 0, 0, 0, 0, null, '0.1.0', now())"
                )
            )
            await conn.execute(
                text(
                    "insert into session_durability (activity_id, sport, "
                    "status, ef_first_half, ef_second_half, decoupling, "
                    "decoupling_pct, within_reference_band, "
                    "reference_band, intensity_first_half, "
                    "intensity_second_half, intensity_drift, "
                    "max_intensity_drift, n_samples, n_first_half, "
                    "n_second_half, detail, engine_version, computed_at) "
                    "values (1, 'bike', 'not_steady', null, null, null, "
                    "null, null, 0.05, 100.0, 300.0, 2.0, 0.15, 100, 50, "
                    "50, 'rejected: not steady', '0.1.0', now())"
                )
            )
        async with engine.connect() as conn:
            signal_key = (
                await conn.execute(
                    text(
                        "select signals->0->>'key' from readiness_snapshot "
                        "where athlete_id = 1"
                    )
                )
            ).scalar_one()
            percentages = (
                await conn.execute(
                    text(
                        "select percentages from weekly_intensity "
                        "where sport = 'bike'"
                    )
                )
            ).scalar_one()
            drift = (
                await conn.execute(
                    text(
                        "select intensity_drift from session_durability "
                        "where status = 'not_steady'"
                    )
                )
            ).scalar_one()
        assert signal_key == "hrv_ln_rmssd"
        assert percentages is None
        assert drift == 2.0

        # Downgrade one step removes the RID-10 tables again.
        await _alembic(command.downgrade, cfg, PREVIOUS_HEAD)
        for table in NEW_TABLES:
            assert not await _table_exists(engine, table)

        # Re-upgrading makes them usable again.
        await _alembic(command.upgrade, cfg, "head")
        for table in NEW_TABLES:
            assert await _table_exists(engine, table)
