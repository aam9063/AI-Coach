"""Celery sync task entry point (ODD task ING-8, PROJECT_BRIEF §12.2).

Single Celery task wrapping the ING-7 backfill so the scheduler (Feature 10)
can trigger the Intervals.icu sync. Contract:

- **Entry point**: ``sync_intervals(days, end_date)`` runs the async
  :func:`app.ingest.backfill.backfill` via :func:`asyncio.run` in
  :func:`run_sync`. With no injected dependencies it builds its own engine
  and session factory (from ``Settings.database_url``) and the sync
  ``IntervalsClient`` (created and closed around the run) — the task owns
  the full lifecycle because Celery workers run sync code in their own
  processes. Both seams (``session_factory``, ``client``) can be injected
  for tests, in which case nothing is constructed here.
- **Summary**: the task returns a JSON-serializable dict with the window
  count, aggregated counts (activities/streams/wellness synced and failed)
  and the failed windows (``ok=False`` + ``failed_windows``), mirroring the
  ING-7 :class:`~app.ingest.backfill.BackfillResult`.
- **Failure contract**: a *window-level* API failure (``IntervalsError``
  inside one day window) is reported in the summary, not raised — the task
  completes successfully with ``ok=False``. Any *unexpected* exception
  (database outage, bug) propagates out of the task body and surfaces as a
  Celery ``FAILURE`` state (``result.state == "FAILURE"`` in eager mode).
- **Registration**: the worker discovers this module through the explicit
  ``include=["app.scheduler.tasks"]`` on the app in
  :mod:`app.scheduler.celery_app` (the Compose command loads the app module
  directly, so ``include`` — not ``autodiscover_tasks`` — is the verifiable
  option). The beat schedule for this task arrives with Feature 10; only
  the entry point is in scope for ING-8.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db.session import create_db_engine, make_session_factory
from app.ingest.backfill import BackfillResult, backfill
from app.ingest.client import IntervalsClient
from app.ingest.sync import IntervalsClientProtocol
from app.scheduler.celery_app import celery_app

# Module-level seam so tests can replace the backfill without the real
# client/database being touched.
_backfill = backfill


def _summary(
    result: BackfillResult, *, days: int, end_date: str | None
) -> dict[str, Any]:
    """Build the JSON-serializable task summary from a BackfillResult."""
    return {
        "days": days,
        "end_date": end_date,
        "windows": len(result.windows),
        "ok": result.ok,
        "activities_synced": result.total.activities_synced,
        "activities_failed": result.total.activities_failed,
        "streams_synced": result.total.streams_synced,
        "streams_skipped": result.total.streams_skipped,
        "streams_failed": result.total.streams_failed,
        "wellness_synced": result.total.wellness_synced,
        "wellness_failed": result.total.wellness_failed,
        "failed_windows": [
            {"oldest": window.oldest, "newest": window.newest, "error": window.error}
            for window in result.windows
            if not window.ok
        ],
    }


async def _run_sync(
    days: int,
    end_date: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    client: IntervalsClientProtocol | None,
    config: Settings,
) -> dict[str, Any]:
    """Run the backfill with the injected (or built) engine/client seams."""
    if client is not None and session_factory is not None:
        result = await _backfill(
            days,
            date.fromisoformat(end_date) if end_date else None,
            session_factory,
            client,
            settings=config,
        )
        return _summary(result, days=days, end_date=end_date)

    # No injected seams: the task owns the full lifecycle — engine, session
    # factory, and the sync client (created and closed around the run).
    engine = create_db_engine(config.database_url)
    try:
        factory = make_session_factory(engine)
        with IntervalsClient(config) as intervals_client:
            result = await _backfill(
                days,
                date.fromisoformat(end_date) if end_date else None,
                factory,
                intervals_client,
                settings=config,
            )
    finally:
        await engine.dispose()
    return _summary(result, days=days, end_date=end_date)


def run_sync(
    days: int,
    end_date: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: IntervalsClientProtocol | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the N-day sync synchronously; returns the JSON-serializable summary.

    Thin synchronous wrapper used by the Celery task; also the in-process
    entry point for tests (inject both ``session_factory`` and ``client``
    to run without a database or network).
    """
    config = settings or get_settings()
    return asyncio.run(_run_sync(days, end_date, session_factory, client, config))


@celery_app.task(name="app.scheduler.tasks.sync_intervals")  # type: ignore[untyped-decorator]
def sync_intervals(
    days: int = 30,
    end_date: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: IntervalsClientProtocol | None = None,
) -> dict[str, Any]:
    """Celery task: backfill/sync ``days`` days of Intervals.icu data.

    Returns a JSON-serializable summary (see :func:`run_sync`); window-level
    failures are reported inside the summary, unexpected exceptions fail the
    task. The ``session_factory``/``client`` keyword seams are for tests and
    never set by the scheduler.
    """
    return run_sync(
        days,
        end_date,
        session_factory=session_factory,
        client=client,
    )
