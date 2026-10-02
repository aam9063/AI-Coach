"""ING-7 RED: N-day backfill command tests (PROJECT_BRIEF §12.2).

Documented ING-7 contract (these tests are its executable specification):

- **Windows**: the backfill uses one sync window per day (``oldest ==
  newest``), walked oldest→newest. Exactly ``days`` windows cover exactly
  N days ending at the explicit end date (or "today" in the configured
  timezone, Europe/Madrid per §14, when omitted) with no gaps or
  overlaps. One day is the smallest retryable unit: ``sync_date_range``
  is idempotent (ING-5) so a retried day re-fetches nothing already
  persisted, and a partial failure invalidates only that day.
- **One sync per window**: ``sync_date_range`` is called exactly once per
  day window (asserted via the client's call recorder — the sync calls
  ``list_activities`` exactly once per invocation), and per-window counts
  are aggregated into one :class:`BackfillResult` summary whose per-window
  records keep the individual ``SyncResult`` counts.
- **Logging**: every completed window logs its counts at INFO; every
  failed window logs a clear ERROR including the window dates and error.
- **Partial failures**: a window whose sync raises an ``IntervalsError``
  (e.g. ``IntervalsHTTPError`` from ``list_activities``) is reported in
  ``BackfillResult.windows[i].error``; ``backfill()`` never raises for
  window-level API failures and continues with the remaining windows.
  ``backfill()`` returns the summary; the CLI entry point ``main()``
  exits with status 1 when any window failed (0 when all succeeded).
- **Pacing** (§5.1, ≤10 req/s) is enforced inside ``sync_date_range``'s
  Pacer and the backfill forwards its own clock/sleep without bypassing
  it.
- **Entry points**: ``async backfill(...)`` is the ING-8 reuse surface
  (Celery task will call it); ``main(argv)`` is the argparse CLI wrapper
  (``python -m app.ingest.backfill --days N [--end YYYY-MM-DD]``),
  exercised here in-process with an injected client/session factory —
  no real network.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.ingest import backfill as backfill_module
from app.ingest.backfill import backfill, compute_day_windows, main
from app.ingest.exceptions import IntervalsServerError
from app.ingest.models import Activity

FAST_SETTINGS = Settings(intervals_min_request_interval_s=0.0)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _unused_factory() -> AsyncSession:
    """Session factory that must never be called (no items => no DB work)."""
    raise AssertionError("backfill with no items must not touch the database")


class DayWindowClient:
    """Duck-typed IntervalsClient: activities filtered to the queried day.

    Records every ``list_activities`` call so tests can assert that the
    backfill ran exactly one sync per day window with the expected dates.
    """

    def __init__(
        self,
        activities_by_day: dict[date, list[Activity]] | None = None,
        *,
        fail_on_day: date | None = None,
    ) -> None:
        self._by_day = activities_by_day or {}
        self._fail_on_day = fail_on_day
        self.window_calls: list[tuple[str, str]] = []

    def list_activities(self, oldest: str, newest: str) -> list[Activity]:
        self.window_calls.append((oldest, newest))
        if self._fail_on_day is not None and oldest == self._fail_on_day.isoformat():
            raise IntervalsServerError(500, "backfill test outage")
        start, end = date.fromisoformat(oldest), date.fromisoformat(newest)
        return [
            activity
            for day in sorted(self._by_day)
            if start <= day <= end
            for activity in self._by_day[day]
        ]

    def get_streams(self, activity_id: int) -> list[Any]:
        return []

    def get_wellness(self, oldest: str, newest: str) -> list[Any]:
        return []

    def download_fit_file(self, activity_id: int) -> bytes:
        return b""


class AlwaysFailingClient:
    """Duck-typed client whose API calls all fail (partial-failure CLI)."""

    def list_activities(self, oldest: str, newest: str) -> list[Activity]:
        raise IntervalsServerError(500, "outage")

    def get_streams(self, activity_id: int) -> list[Any]:
        raise IntervalsServerError(500, "outage")

    def get_wellness(self, oldest: str, newest: str) -> list[Any]:
        raise IntervalsServerError(500, "outage")

    def download_fit_file(self, activity_id: int) -> bytes:
        raise IntervalsServerError(500, "outage")


def _activity(aid: int, day: date) -> Activity:
    return Activity.model_validate(
        {
            "id": aid,
            "name": f"Ride {aid}",
            "type": "Ride",
            "start_date": f"{day.isoformat()}T07:30:00+00:00",
            "start_date_local": f"{day.isoformat()}T09:30:00+01:00",
            "distance": 10000.0,
            "moving_time": 3600,
        }
    )


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


def _backfill_log_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "app.ingest.backfill"]


# ---------------------------------------------------------------------------
# Day-window computation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "end_date"),
    [
        (1, date(2026, 1, 31)),
        (5, date(2026, 1, 19)),
        # Month boundary: window crosses into the previous month.
        (32, date(2026, 1, 15)),
        # Year boundary: window crosses into the previous year.
        (10, date(2026, 1, 3)),
    ],
)
def test_day_windows_cover_exactly_n_days_no_gaps_or_overlaps(
    days: int, end_date: date
) -> None:
    windows = compute_day_windows(days, end_date)

    assert len(windows) == days
    # Consecutive one-day windows, oldest first, ending at the end date.
    assert windows[-1][1] == end_date
    assert windows[0][0] == date.fromordinal(end_date.toordinal() - days + 1)
    previous_end = None
    for oldest, newest in windows:
        assert oldest == newest, "one sync window per day"
        if previous_end is not None:
            assert oldest.toordinal() == previous_end.toordinal() + 1, "no gaps"
        previous_end = newest
    covered = {oldest for oldest, _ in windows}
    assert len(covered) == days, "no overlapping days"
    # The covered days are exactly the N days ending at end_date.
    expected = {
        date.fromordinal(end_date.toordinal() - offset) for offset in range(days)
    }
    assert covered == expected


def test_day_windows_default_to_today_in_configured_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit end date, "today" is injected (fixed for tests)."""
    fixed_today = date(2026, 3, 15)
    monkeypatch.setattr(backfill_module, "today_in_zone", lambda tz: fixed_today)

    windows = compute_day_windows(3)

    assert windows == [
        (date(2026, 3, 13), date(2026, 3, 13)),
        (date(2026, 3, 14), date(2026, 3, 14)),
        (date(2026, 3, 15), date(2026, 3, 15)),
    ]


def test_day_windows_reject_non_positive_days() -> None:
    with pytest.raises(ValueError, match="days"):
        compute_day_windows(0)


# ---------------------------------------------------------------------------
# One sync per window + count aggregation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_backfill_uses_sync_once_per_day_window() -> None:
    """sync_date_range runs once per window (list_activities once per sync)."""
    client = DayWindowClient()

    result = await backfill(
        3,
        date(2026, 1, 31),
        _unused_factory,  # type: ignore[arg-type]
        client,
        settings=FAST_SETTINGS,
    )

    assert client.window_calls == [
        ("2026-01-29", "2026-01-29"),
        ("2026-01-30", "2026-01-30"),
        ("2026-01-31", "2026-01-31"),
    ]
    assert result.ok


@pytest.mark.anyio
async def test_backfill_aggregates_counts_across_windows(
    db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    days_with_activities = {date(2026, 1, 15): 1, date(2026, 1, 17): 2}
    client = DayWindowClient(
        {
            day: [_activity(aid, day)]
            for day, aid in days_with_activities.items()
        }
    )

    with caplog.at_level(logging.INFO, logger="app.ingest.backfill"):
        result = await backfill(
            5,
            date(2026, 1, 19),
            _factory(db_engine),
            client,
            settings=FAST_SETTINGS,
        )

    # One window per day, oldest→newest.
    assert [oldest for oldest, _ in client.window_calls] == [
        "2026-01-15",
        "2026-01-16",
        "2026-01-17",
        "2026-01-18",
        "2026-01-19",
    ]
    assert len(result.windows) == 5
    assert result.ok

    # Per-window records keep the individual counts.
    assert result.windows[0].counts.activities_synced == 1
    assert result.windows[1].counts.activities_synced == 0
    assert result.windows[2].counts.activities_synced == 1
    assert result.windows[3].counts.activities_synced == 0

    # One aggregated summary across all windows.
    assert result.total.activities_synced == 2
    assert result.total.activities_failed == 0
    assert result.total.wellness_failed == 0

    # Per-window counts are logged (INFO).
    window_logs = [
        record.getMessage() for record in _backfill_log_records(caplog)
        if record.levelno == logging.INFO
    ]
    assert len(window_logs) == 5
    assert any("2026-01-15" in message and "activities=1" in message for message in window_logs)


@pytest.mark.anyio
async def test_backfill_default_end_is_injected_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_today = date(2026, 3, 15)
    monkeypatch.setattr(backfill_module, "today_in_zone", lambda tz: fixed_today)
    client = DayWindowClient()

    await backfill(
        2,
        None,
        _unused_factory,  # type: ignore[arg-type]
        client,
        settings=FAST_SETTINGS,
    )

    assert client.window_calls == [
        ("2026-03-14", "2026-03-14"),
        ("2026-03-15", "2026-03-15"),
    ]


# ---------------------------------------------------------------------------
# Partial failures: report, continue, non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_backfill_continues_after_window_failure_and_reports(
    db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    client = DayWindowClient(
        {
            date(2026, 1, 15): [_activity(1, date(2026, 1, 15))],
            date(2026, 1, 19): [_activity(2, date(2026, 1, 19))],
        },
        fail_on_day=date(2026, 1, 17),
    )

    with caplog.at_level(logging.ERROR, logger="app.ingest.backfill"):
        result = await backfill(
            5,
            date(2026, 1, 19),
            _factory(db_engine),
            client,
            settings=FAST_SETTINGS,
        )

    # The failing window did not abort the run: all 5 windows attempted.
    assert len(client.window_calls) == 5
    assert len(result.windows) == 5

    # The failed window is reported with its error; neighbours succeed.
    failed = result.windows[2]
    assert failed.oldest == "2026-01-17"
    assert not failed.ok
    assert failed.error is not None and "HTTP 500" in failed.error

    assert result.ok is False
    assert result.total.activities_synced == 2
    assert result.total.activities_failed == 0

    # Clear error log for the failed window.
    error_logs = [
        record.getMessage()
        for record in _backfill_log_records(caplog)
        if record.levelno == logging.ERROR
    ]
    assert len(error_logs) == 1
    assert "2026-01-17" in error_logs[0]
    assert "HTTP 500" in error_logs[0]


def test_main_exits_nonzero_when_window_fails(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.ingest.backfill"):
        exit_code = main(
            ["--days", "2", "--end", "2026-01-31"],
            session_factory=_unused_factory,  # type: ignore[arg-type]
            client=AlwaysFailingClient(),
            settings=FAST_SETTINGS,
        )

    assert exit_code == 1
    out = capsys.readouterr()
    assert "0/2" in out.out
    assert "FAILED" in out.err


# ---------------------------------------------------------------------------
# CLI wiring (in-process, injected dependencies, no real network)
# ---------------------------------------------------------------------------


def test_main_success_prints_summary_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = DayWindowClient()

    exit_code = main(
        ["--days", "2", "--end", "2026-01-31"],
        session_factory=_unused_factory,  # type: ignore[arg-type]
        client=client,
        settings=FAST_SETTINGS,
    )

    assert exit_code == 0
    assert client.window_calls == [
        ("2026-01-30", "2026-01-30"),
        ("2026-01-31", "2026-01-31"),
    ]
    out = capsys.readouterr().out
    assert "2/2" in out
    assert "activities=0" in out


def test_main_rejects_non_positive_days() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["--days", "0", "--end", "2026-01-31"],
            session_factory=_unused_factory,  # type: ignore[arg-type]
            client=DayWindowClient(),
            settings=FAST_SETTINGS,
        )
    assert excinfo.value.code == 2
