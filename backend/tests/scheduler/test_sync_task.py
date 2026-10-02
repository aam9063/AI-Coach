"""ING-8 RED: Celery sync task entry point tests (PROJECT_BRIEF §12.2).

Documented ING-8 contract (these tests are its executable specification):

- **Registration**: the task ``sync_intervals`` is registered on the
  tri-coach Celery app under the fully-qualified name
  ``app.scheduler.tasks.sync_intervals``. The worker discovers it through
  the explicit ``include=["app.scheduler.tasks"]`` on the app (verified
  against the Compose worker command, which loads the app module directly);
  importing :mod:`app.scheduler.tasks` (or the app include doing it)
  populates ``celery_app.tasks``.
- **Task body**: the body runs the ING-7 backfill through one asyncio
  entry point and returns a **JSON-serializable summary** with the counts
  (days covered, activities/streams/wellness synced and failed) and the
  failed windows. No network and no database are touched in these tests:
  the backfill function is replaced at the module seam and sentinel
  session-factory/client objects are injected and forwarded untouched.
- **Partial-failure contract**: a window-level failure is *not* a task
  failure — the task completes successfully (eager ``SUCCESS``) and the
  failure is visible in the returned summary (``ok=False`` plus the
  ``failed_windows`` entries), mirroring the ING-7 backfill contract.
- **Unexpected exceptions**: anything other than the backfill's own
  window-level failure reporting (e.g. a database outage, a bug) propagates
  out of the task body and surfaces as a Celery ``FAILURE`` state
  (asserted in eager mode, where it does not re-raise by default).
- **Eager mode**: tests set ``task_always_eager`` so no broker/Redis is
  required.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from app.ingest.backfill import BackfillResult, WindowResult
from app.ingest.sync import SyncResult
from app.scheduler import tasks as scheduler_tasks
from app.scheduler.celery_app import celery_app

TASK_NAME = "app.scheduler.tasks.sync_intervals"
SentinelSeams = tuple["_Sentinel", "_Sentinel"]


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Sentinel:
    """Opaque injected dependency; forwarded to the backfill untouched."""


def make_backfill_result(
    windows: list[WindowResult] | None = None,
) -> BackfillResult:
    """Build a BackfillResult with the given windows and summed totals."""
    result = BackfillResult(windows=windows or [])
    for window in result.windows:
        if window.ok:
            counts = window.counts
            result.total.activities_synced += counts.activities_synced
            result.total.activities_failed += counts.activities_failed
            result.total.streams_synced += counts.streams_synced
            result.total.streams_skipped += counts.streams_skipped
            result.total.streams_failed += counts.streams_failed
            result.total.wellness_synced += counts.wellness_synced
            result.total.wellness_failed += counts.wellness_failed
    return result


def fake_backfill(
    calls: list[dict[str, Any]],
    result: BackfillResult,
    *,
    error: Exception | None = None,
) -> Callable[..., Any]:
    """Async stand-in for app.ingest.backfill.backfill: records the call."""

    async def _backfill(
        days: int,
        end_date: date | None,
        session_factory: Any,
        client: Any,
        **kwargs: Any,
    ) -> BackfillResult:
        calls.append(
            {
                "days": days,
                "end_date": end_date,
                "session_factory": session_factory,
                "client": client,
                **kwargs,
            }
        )
        if error is not None:
            raise error
        return result

    return _backfill


@pytest.fixture
def backfill_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], tuple[_Sentinel, _Sentinel]]:
    """Replace the backfill seam with a recording fake and inject seams.

    Sentinel session-factory/client seams are injected so the task body
    never builds the real engine or ``IntervalsClient`` (no database, no
    network, no API key needed).
    """
    calls: list[dict[str, Any]] = []
    seams = (_Sentinel(), _Sentinel())
    monkeypatch.setattr(
        scheduler_tasks, "_backfill", fake_backfill(calls, make_backfill_result())
    )
    return calls, seams


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_sync_intervals_is_registered_on_the_app() -> None:
    """Importing the tasks module registers the task under its full name."""
    assert TASK_NAME in celery_app.tasks


# ---------------------------------------------------------------------------
# Task body (eager mode, no network / no database)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("eager_celery")
def test_task_runs_backfill_and_returns_json_summary(
    backfill_calls: tuple[list[dict[str, Any]], SentinelSeams],
) -> None:
    """days=3 runs the backfill once and returns a serializable summary."""
    calls, (session_factory, client) = backfill_calls
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 3, "session_factory": session_factory, "client": client}
    )

    assert result.state == "SUCCESS"
    summary = result.get()
    json.dumps(summary)  # JSON-serializable
    assert len(calls) == 1
    assert summary == {
        "days": 3,
        "end_date": None,
        "windows": 0,
        "ok": True,
        "activities_synced": 0,
        "activities_failed": 0,
        "streams_synced": 0,
        "streams_skipped": 0,
        "streams_failed": 0,
        "wellness_synced": 0,
        "wellness_failed": 0,
        "failed_windows": [],
    }


@pytest.mark.usefixtures("eager_celery")
def test_task_forwards_injected_seams_to_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session factory and client are forwarded untouched; none are built."""
    calls: list[dict[str, Any]] = []
    session_factory, client = _Sentinel(), _Sentinel()
    monkeypatch.setattr(
        scheduler_tasks,
        "_backfill",
        fake_backfill(calls, make_backfill_result()),
    )
    summary = scheduler_tasks.sync_intervals.run(
        days=3,
        end_date="2026-02-08",
        session_factory=session_factory,
        client=client,
    )

    assert summary["days"] == 3
    assert summary["end_date"] == "2026-02-08"
    assert len(calls) == 1
    call = calls[0]
    assert call["days"] == 3
    # The string end_date is converted to a date before reaching the backfill.
    assert call["end_date"] == date(2026, 2, 8)
    assert call["session_factory"] is session_factory
    assert call["client"] is client
    # The backfill receives the settings it needs; the real engine/client
    # constructors must never run because both seams were injected.
    assert "settings" in call


@pytest.mark.usefixtures("eager_celery")
def test_task_summary_counts_reflect_backfill_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counts in the summary come from the backfill's aggregated totals."""
    windows = [
        WindowResult(
            oldest="2026-02-06",
            newest="2026-02-06",
            counts=SyncResult(
                activities_synced=2,
                streams_synced=4,
                streams_skipped=1,
                wellness_synced=1,
            ),
        ),
        WindowResult(
            oldest="2026-02-07",
            newest="2026-02-07",
            counts=SyncResult(activities_synced=1, streams_synced=2),
        ),
    ]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_tasks,
        "_backfill",
        fake_backfill(calls, make_backfill_result(windows)),
    )
    summary = scheduler_tasks.sync_intervals.run(
        days=2,
        session_factory=_Sentinel(),
        client=_Sentinel(),
    )

    assert summary["windows"] == 2
    assert summary["ok"] is True
    assert summary["activities_synced"] == 3
    assert summary["streams_synced"] == 6
    assert summary["streams_skipped"] == 1
    assert summary["wellness_synced"] == 1
    assert summary["failed_windows"] == []


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("eager_celery")
def test_window_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed window shows up in the summary; the task still succeeds."""
    windows = [
        WindowResult(
            oldest="2026-02-07",
            newest="2026-02-07",
            counts=SyncResult(activities_synced=1),
        ),
        WindowResult(
            oldest="2026-02-08",
            newest="2026-02-08",
            error="IntervalsHTTPError: 500",
        ),
    ]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_tasks,
        "_backfill",
        fake_backfill(calls, make_backfill_result(windows)),
    )
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={
            "days": 2,
            "session_factory": _Sentinel(),
            "client": _Sentinel(),
        }
    )

    assert result.state == "SUCCESS"
    summary = result.get()
    assert summary["ok"] is False
    assert summary["windows"] == 2
    assert summary["activities_synced"] == 1
    assert summary["failed_windows"] == [
        {"oldest": "2026-02-08", "newest": "2026-02-08", "error": "IntervalsHTTPError: 500"}
    ]


@pytest.mark.usefixtures("eager_celery")
def test_unexpected_exception_fails_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception surfaces as a Celery FAILURE state."""
    monkeypatch.setattr(
        scheduler_tasks,
        "_backfill",
        fake_backfill([], make_backfill_result(), error=RuntimeError("db down")),
    )
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 1, "session_factory": _Sentinel(), "client": _Sentinel()}
    )

    assert result.state == "FAILURE"
    assert isinstance(result.result, RuntimeError)
