"""Celery task: readiness recompute (beat: ``readiness-recompute``).

**The fix for the gap the live run recorded** (SCH-10, ODD task
``scheduled-messages``): the beat declared sync, load recompute, brief,
review and weekly report, but NOTHING computed readiness — so
``readiness_snapshot`` had no row for today and the morning brief could
never get past its own gather ("no readiness snapshot for ...: the brief
is skipped, never composed from invented data"). This entry schedules the
readiness computation itself.

- **Same function as the tool path** (the one-seam rule): the task body
  calls :func:`app.services.readiness.recompute_readiness` — the EXACT
  service the ``log_subjective`` agent tool and the
  ``python -m app.db.engine_outputs`` CLI use. No second readiness
  pipeline exists; the constants come from Settings via
  :func:`app.db.engine_readiness_config.readiness_constants_from_settings`
  (RID-10a), never re-hardcoded here. One commit at the end, like the
  ``run_recompute`` runner of the daily-load task.
- **Scheduled as its OWN beat entry** (``readiness-recompute``, :28/:58),
  not as an extension of the ``daily-load-recompute`` task it depends on:
  readiness reads the ``daily_load`` ``combined`` TSB rows, so it must run
  AFTER the :25/:55 load recompute — but nesting it inside the load task
  body would couple the two jobs' failures (a readiness error would fail
  the load task too, or demand a silent try/except swallow the failure
  contract forbids). Beat entries are independent: each failure surfaces
  as its own Celery ``FAILURE`` state, never silently swallowed. The
  :28/:58 offset lands before the 07:30 brief, which consumes the fresh
  snapshot.
- **Bounded**: the window defaults to and the beat entry pins
  ``READINESS_WINDOW_DAYS = 30`` — the same trailing window as the sync
  and the load recompute, never years of reprocessing every half hour.
  A non-positive window is rejected by the service's own contract and
  fails the task loudly.
- **Idempotent**: the service upserts one ``readiness_snapshot`` row per
  ``(athlete_id, date)``; re-running the task leaves the same rows with
  the same content (proven against the dedicated test database by
  ``tests/scheduler/test_readiness_task.py``). No schema change: the rows
  and the service predate this entry.
- **Summary**: the task returns a JSON-serializable dict (window bounds,
  days considered, rows upserted and every skipped day with its reason),
  mirroring the service's :class:`~app.services.readiness.ReadinessReport`.
- **Offline tests**: inject ``session_factory`` (test-only seam; the
  scheduler never sets it) or monkeypatch the ``_recompute`` module seam —
  no database and no network needed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db.engine_readiness_config import readiness_constants_from_settings
from app.db.session import create_db_engine, make_session_factory
from app.scheduler.celery_app import READINESS_WINDOW_DAYS, celery_app
from app.services.readiness import ReadinessReport
from app.services.readiness import recompute_readiness as recompute_readiness_service

# Module-level seam so tests can replace the service without a database.
# ALIAS-PINNED: the seam IS the service function the tool path calls
# (identity asserted by tests/scheduler/test_readiness_task.py).
_recompute = recompute_readiness_service

__all__ = [
    "READINESS_WINDOW_DAYS",
    "recompute_readiness",
    "run_readiness_recompute",
    "run_readiness_recompute_sync",
]


def _summary(report: ReadinessReport, *, days: int, athlete_id: int) -> dict[str, Any]:
    """Build the JSON-serializable task summary from a ReadinessReport."""
    return {
        "days": days,
        "athlete_id": athlete_id,
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "engine_version": report.engine_version,
        "days_considered": report.days_considered,
        "rows_upserted": report.rows_upserted,
        "skipped": [
            {"date": skip.date.isoformat(), "reason": skip.reason}
            for skip in report.skipped
        ],
    }


async def run_readiness_recompute(
    settings: Settings,
    *,
    days: int = READINESS_WINDOW_DAYS,
    athlete_id: int = 1,
    window_end: dt.date,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ReadinessReport:
    """Run one readiness recompute-and-persist cycle from the settings.

    Shared runner for BOTH entry points — the on-demand CLI job
    (``python -m app.scheduler.cli readiness_recompute``) and the
    scheduled Celery task (below) — so their behaviour cannot drift: the
    settings-sourced engine constants, the service call and the single
    final commit all happen here, once.

    With no injected ``session_factory`` the runner owns the full engine
    lifecycle (built from ``settings.database_url``, disposed afterwards).
    The seam exists for tests, which run fully offline with it; the
    scheduler never sets it.
    """
    constants = readiness_constants_from_settings(settings)

    async def _once(session_factory: async_sessionmaker[AsyncSession]) -> ReadinessReport:
        async with session_factory() as session:
            report = await _recompute(
                session,
                athlete_id=athlete_id,
                window_end=window_end,
                days=days,
                engine_version=settings.engine_version,
                **constants,
            )
            await session.commit()
            return report

    if session_factory is not None:
        return await _once(session_factory)

    engine = create_db_engine(settings.database_url)
    try:
        return await _once(make_session_factory(engine))
    finally:
        await engine.dispose()


def run_readiness_recompute_sync(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    days: int = READINESS_WINDOW_DAYS,
    athlete_id: int = 1,
    window_end: str | None = None,
) -> dict[str, Any]:
    """Synchronous entry point the Celery task and the CLI call.

    Every dependency can be injected for tests; the defaults derive from
    configuration (``get_settings``, the async DB engine) so the
    production path needs no scaffolding. ``window_end`` is an ISO date
    string (default: today, UTC — the same default as the engine-outputs
    CLI); ``days`` is the bounded trailing window.
    """
    config = settings if settings is not None else get_settings()
    end = (
        dt.date.fromisoformat(window_end)
        if window_end is not None
        else dt.datetime.now(dt.UTC).date()
    )
    report = asyncio.run(
        run_readiness_recompute(
            config,
            days=days,
            athlete_id=athlete_id,
            window_end=end,
            session_factory=session_factory,
        )
    )
    return _summary(report, days=days, athlete_id=athlete_id)


@celery_app.task(name="app.scheduler.readiness_tasks.recompute_readiness")  # type: ignore[untyped-decorator]
def recompute_readiness(
    days: int = READINESS_WINDOW_DAYS,
    athlete_id: int = 1,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    """Celery task: recompute and persist the readiness snapshots.

    Runs :func:`app.services.readiness.recompute_readiness` (the tool
    path's own seam) for a trailing window of ``days`` calendar days
    ending at ``window_end`` (default: today, UTC). Returns a
    JSON-serializable summary; the ``session_factory``/``window_end``
    keyword seams are for tests and are never set by the beat schedule.
    """
    return run_readiness_recompute_sync(
        session_factory=session_factory,
        days=days,
        athlete_id=athlete_id,
        window_end=window_end,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("run via celery worker/beat, not directly")
