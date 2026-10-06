"""Backfill command for N days (ODD task ING-7, PROJECT_BRIEF §12.2).

Splits an N-day range into one sync window per day and runs the ING-5
idempotent orchestrator once per window:

- **Windows**: consecutive single-day windows walked oldest→newest, ending
  at the explicit ``end_date`` or "today" in the configured timezone
  (Europe/Madrid per §14) when omitted. One day is the smallest retryable
  unit: ``sync_date_range`` is idempotent so a retried day re-fetches
  nothing already persisted (§12.2 acceptance), and a partial failure
  invalidates only that day.
- **Pacing (§5.1)**: the ≤10 req/s minimum interval between client calls is
  enforced inside ``sync_date_range``'s Pacer; the backfill forwards its
  own clock/sleep and never bypasses it.
- **Partial-failure contract**: a window whose sync raises an
  ``IntervalsError`` (e.g. ``IntervalsHTTPError`` from ``list_activities``)
  is recorded in :attr:`BackfillResult.windows[i].error` and logged at
  ERROR with the window dates; the remaining windows still run and
  ``backfill()`` itself never raises for window-level API failures. The
  caller inspects :attr:`BackfillResult.ok`; the CLI entry point ``main``
  exits 1 when any window failed (0 otherwise).
- **Counts**: every completed window logs its per-endpoint counts at INFO,
  and all windows are aggregated into one :class:`BackfillResult` whose
  per-window records keep the individual :class:`~app.ingest.sync.SyncResult`.

Entry points:

- :func:`backfill` — async, dependency-injected (session factory, client,
  settings); the reuse surface for the ING-8 Celery task.
- :func:`main` — argparse CLI (``python -m app.ingest.backfill --days N
  [--end YYYY-MM-DD]``) that builds the real client/session factory from
  settings; accepts the same injected dependencies so tests run it
  in-process with fakes (no real network).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db.session import create_db_engine, make_session_factory
from app.ingest.client import IntervalsClient
from app.ingest.exceptions import IntervalsError
from app.ingest.sync import Clock, IntervalsClientProtocol, Sleep, SyncResult, sync_date_range

logger = logging.getLogger(__name__)


def today_in_zone(timezone_name: str) -> date:
    """Today's date in the configured timezone (§14: Europe/Madrid)."""
    return datetime.now(ZoneInfo(timezone_name)).date()


def compute_day_windows(
    days: int, end_date: date | None = None, *, tz: str = "Europe/Madrid"
) -> list[tuple[date, date]]:
    """Compute the per-day backfill windows covering exactly ``days`` days.

    Returns consecutive one-day windows ``(oldest, newest)`` walked
    oldest→newest with no gaps or overlaps, ending at ``end_date`` (or
    "today" in ``tz`` when omitted). Raises ``ValueError`` for non-positive
    ``days``.
    """
    if days < 1:
        raise ValueError(f"days must be a positive integer, got {days}")
    end = end_date or today_in_zone(tz)
    start = date.fromordinal(end.toordinal() - days + 1)
    return [(day, day) for day in (start + timedelta(days=offset) for offset in range(days))]


@dataclass
class WindowResult:
    """Outcome of one day window: counts, or an error when it failed."""

    oldest: str
    newest: str
    error: str | None = None
    counts: SyncResult = field(default_factory=SyncResult)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class BackfillResult:
    """Aggregated backfill summary: per-window records plus totals."""

    windows: list[WindowResult] = field(default_factory=list)
    total: SyncResult = field(default_factory=SyncResult)

    @property
    def ok(self) -> bool:
        return all(window.ok for window in self.windows)


def _add_counts(total: SyncResult, counts: SyncResult) -> None:
    total.activities_synced += counts.activities_synced
    total.activities_failed += counts.activities_failed
    total.streams_synced += counts.streams_synced
    total.streams_skipped += counts.streams_skipped
    total.streams_failed += counts.streams_failed
    total.wellness_synced += counts.wellness_synced
    total.wellness_failed += counts.wellness_failed


async def backfill(
    days: int,
    end_date: date | None,
    session_factory: async_sessionmaker[AsyncSession],
    client: IntervalsClientProtocol,
    *,
    settings: Settings | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> BackfillResult:
    """Backfill ``days`` days ending at ``end_date`` (default: today).

    Runs ``sync_date_range`` once per day window with the injected
    dependencies, aggregates the per-window counts into one
    :class:`BackfillResult` and logs per-window counts. Window-level API
    failures are reported, never raised (see module docstring).
    """
    config = settings or get_settings()
    windows = compute_day_windows(days, end_date, tz=config.timezone)
    result = BackfillResult()

    for oldest, newest in windows:
        oldest_str, newest_str = oldest.isoformat(), newest.isoformat()
        try:
            counts = await sync_date_range(
                oldest_str,
                newest_str,
                session_factory,
                client,
                settings=config,
                clock=clock,
                sleep=sleep,
            )
        except IntervalsError as exc:
            logger.error(
                "backfill window %s..%s FAILED: %s", oldest_str, newest_str, exc
            )
            result.windows.append(
                WindowResult(oldest=oldest_str, newest=newest_str, error=str(exc))
            )
            continue
        logger.info(
            "backfill window %s..%s: activities=%d (failed=%d) streams=%d "
            "(skipped=%d, failed=%d) wellness=%d (failed=%d)",
            oldest_str,
            newest_str,
            counts.activities_synced,
            counts.activities_failed,
            counts.streams_synced,
            counts.streams_skipped,
            counts.streams_failed,
            counts.wellness_synced,
            counts.wellness_failed,
        )
        result.windows.append(
            WindowResult(oldest=oldest_str, newest=newest_str, counts=counts)
        )
        _add_counts(result.total, counts)

    return result


def build_parser() -> argparse.ArgumentParser:
    """Build the backfill CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest.backfill",
        description=(
            "Backfill N days of Intervals.icu data "
            "(one idempotent sync window per day)."
        ),
    )
    parser.add_argument(
        "--days",
        type=_positive_int,
        required=True,
        metavar="N",
        help="number of days to backfill, ending at --end (default: today)",
    )
    parser.add_argument(
        "--end",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="last day of the backfill window, inclusive (default: today)",
    )
    return parser


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("--days must be a positive integer")
    return parsed


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date (expected YYYY-MM-DD): {value!r}"
        ) from None


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client: IntervalsClientProtocol | None = None,
    settings: Settings | None = None,
) -> int:
    """CLI entry point; returns 0 on full success, 1 on any failed window.

    With no injected dependencies the real client and session factory are
    built from settings. Returns 2 on argument errors (argparse).
    """
    args = build_parser().parse_args(argv)
    config = settings or get_settings()
    result = asyncio.run(
        _run_backfill(args.days, args.end, config, session_factory, client)
    )
    _report(result)
    return 0 if result.ok else 1


async def _run_backfill(
    days: int,
    end_date: date | None,
    config: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None,
    client: IntervalsClientProtocol | None,
) -> BackfillResult:
    if session_factory is None:
        engine = create_db_engine(config.database_url)
        try:
            return await _backfill_with_client(
                days, end_date, config, make_session_factory(engine), client
            )
        finally:
            await engine.dispose()
    return await _backfill_with_client(days, end_date, config, session_factory, client)


async def _backfill_with_client(
    days: int,
    end_date: date | None,
    config: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    client: IntervalsClientProtocol | None,
) -> BackfillResult:
    if client is not None:
        return await backfill(days, end_date, session_factory, client, settings=config)
    with IntervalsClient(config) as intervals_client:
        return await backfill(
            days, end_date, session_factory, intervals_client, settings=config
        )


def _report(result: BackfillResult) -> None:
    """Print the counts summary; failed windows go to stderr with details."""
    failed = [window for window in result.windows if not window.ok]
    total = result.total
    print(
        f"Backfill: {len(result.windows) - len(failed)}/{len(result.windows)} "
        f"windows ok; activities={total.activities_synced} "
        f"(failed={total.activities_failed}) streams={total.streams_synced} "
        f"(skipped={total.streams_skipped}, failed={total.streams_failed}) "
        f"wellness={total.wellness_synced} (failed={total.wellness_failed})"
    )
    for window in failed:
        print(
            f"FAILED window {window.oldest}..{window.newest}: {window.error}",
            file=sys.stderr,
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
