"""Celery task: daily TSS/CTL/ATL/TSB recompute (beat: ``daily-load-recompute``).

Scheduled daily at **06:55 Europe/Madrid** (OWNER CHOICE, 2026-10-08 — see
:mod:`app.scheduler.celery_app` for the cadence rationale): strictly after
the ``daily-intervals-sync`` entry, so the recompute sees whatever the
morning sync just brought in.

- **Same function as the CLI** (LOAD-10): the task body calls
  :func:`app.db.daily_load.run_recompute` — the exact runner the
  ``python -m app.db.daily_load`` CLI uses (settings-sourced thresholds,
  TRIMP coefficients and engine constants, one final commit). No logic is
  duplicated here and the task never shells out to the CLI. The runner is
  idempotent: re-running upserts the same ``daily_load`` rows.
- **Summary**: the task returns a JSON-serializable dict (window bounds,
  per-day/per-sport counts, persisted rows, load methods used and every
  skipped activity with its reason), mirroring the CLI report.
- **Failure contract**: an unexpected exception (database outage, unknown
  ``engine_trimp_sex``) propagates out of the task body and surfaces as a
  Celery ``FAILURE`` state — never silently swallowed. Beat entries are
  independent: a failing ``daily-intervals-sync`` does NOT suppress this
  entry; the recompute simply runs against the last successfully synced
  data.
- **Offline tests**: inject ``session_factory`` (test-only seam; the
  scheduler never sets it) or monkeypatch the ``_run_recompute`` module
  seam — no database and no network needed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db.daily_load import run_recompute
from app.scheduler.celery_app import celery_app
from app.services.daily_load import DailyLoadReport

# Module-level seam so tests can replace the runner without a database.
_run_recompute = run_recompute

__all__ = ["recompute_daily_load"]


def _summary(
    report: DailyLoadReport, *, days: int, athlete_id: int
) -> dict[str, Any]:
    """Build the JSON-serializable task summary from a DailyLoadReport."""
    return {
        "days": days,
        "athlete_id": athlete_id,
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "engine_version": report.engine_version,
        "activities_considered": report.activities_considered,
        "rows_upserted": report.rows_upserted,
        "per_day_activities": {
            day.isoformat(): count
            for day, count in report.per_day_activities.items()
        },
        "per_sport_rows": dict(report.per_sport_rows),
        "methods_used": dict(report.methods_used),
        "skipped": [
            {"activity_id": skip.activity_id, "sport": skip.sport, "reason": skip.reason}
            for skip in report.skipped
        ],
    }


@celery_app.task(name="app.scheduler.daily_load_tasks.recompute_daily_load")  # type: ignore[untyped-decorator]
def recompute_daily_load(
    days: int = 30,
    athlete_id: int = 1,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    """Celery task: recompute and persist the daily load (TSS/CTL/ATL/TSB).

    Runs :func:`app.db.daily_load.run_recompute` for a trailing window of
    ``days`` calendar days ending at ``window_end`` (default: today, UTC —
    the same default as the CLI). Returns a JSON-serializable summary; the
    ``session_factory``/``window_end`` keyword seams are for tests and are
    never set by the beat schedule.
    """
    settings: Settings = get_settings()
    end = (
        dt.date.fromisoformat(window_end)
        if window_end is not None
        else dt.datetime.now(dt.UTC).date()
    )
    report = asyncio.run(
        _run_recompute(
            settings,
            days=days,
            athlete_id=athlete_id,
            window_end=end,
            session_factory=session_factory,
        )
    )
    return _summary(report, days=days, athlete_id=athlete_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("run via celery worker/beat, not directly")
