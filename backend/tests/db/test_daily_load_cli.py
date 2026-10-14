"""End-to-end pin of the ``python -m app.db.daily_load`` CLI (LOAD-10).

The daily beat schedule wraps the SAME runner this CLI uses
(:func:`app.db.daily_load.run_recompute`). This test keeps pinning that the
CLI itself still works exactly as before the extraction: exit code 0, the
same human-readable report on stdout, rows persisted, and a second run is
idempotent (same rows, no duplicates).

Runs against the DEDICATED test database (the CLI's ``get_settings`` is
monkeypatched to the test DSN); requires the compose Postgres, offline
otherwise (no network — the recompute reads only stored rows).
"""

from __future__ import annotations

from datetime import UTC, datetime

import anyio
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db import daily_load as cli
from app.db.models import DailyLoadRow
from tests.dbsupport import test_database_url as _test_database_url

pytestmark = pytest.mark.anyio

START = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
HR_PAYLOAD = [150.0] * 4  # gaps excluded -> mean 150.0


def _test_settings() -> Settings:
    """Settings pointed at the dedicated test DB, with owner thresholds set.

    Without thresholds every method would be skipped (§5.1); the ride hrTSS
    path needs lthr/hr_max/hr_rest, exactly like the other persistence tests.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=_test_database_url(),
        athlete_lthr_bpm=169.0,
        athlete_hr_max_bpm=186.0,
        athlete_hr_rest_bpm=65.0,
    )


async def _store_ride(db_session: AsyncSession) -> None:
    from app.db import repository

    activity = await repository.upsert_activity(
        db_session,
        source_id="i-cli-pin-1",
        type="Ride",
        name="Ride",
        start_time=START,
        duration_s=3600,
    )
    await repository.upsert_activity_stream(
        db_session,
        activity_id=activity.id,
        stream_type="hr",
        data=list(HR_PAYLOAD),
    )
    await db_session.commit()


async def _daily_load_rows(db_session: AsyncSession) -> int:
    return int(
        (
            await db_session.execute(select(func.count()).select_from(DailyLoadRow))
        ).scalar_one()
    )


async def test_cli_still_recomputes_prints_report_and_is_idempotent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One ride in, the CLI exits 0, prints its report, persists rows."""
    await _store_ride(db_session)
    monkeypatch.setattr(cli, "get_settings", _test_settings)

    def _run() -> int:
        # Off the event loop: the CLI body calls ``asyncio.run`` itself.
        return cli.main(["--days", "1", "--end", "2026-07-01"])

    exit_code = await anyio.to_thread.run_sync(_run)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert out.startswith("daily_load recompute: window 2026-07-01..2026-07-01")
    assert "rows upserted: " in out
    rows_after_first_run = await _daily_load_rows(db_session)
    assert rows_after_first_run > 0

    # Idempotent: a second identical run upserts the same rows, no dupes.
    exit_code = await anyio.to_thread.run_sync(_run)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "rows upserted: " in out
    assert await _daily_load_rows(db_session) == rows_after_first_run
