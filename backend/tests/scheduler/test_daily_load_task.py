"""Celery task wrapping the daily-load recompute (beat: daily-load-recompute).

Executable specification of ``app.scheduler.daily_load_tasks``:

- **Registration**: the task is registered under the fully-qualified name
  ``app.scheduler.daily_load_tasks.recompute_daily_load`` (pinned by the
  beat-schedule tests too).
- **Same function as the CLI**: the task body calls
  :func:`app.db.daily_load.run_recompute` — the exact runner the
  ``python -m app.db.daily_load`` CLI uses. No logic is duplicated and the
  task never shells out to the CLI.
- **Body**: with an injected ``session_factory`` seam the task runs fully
  offline and returns a JSON-serializable summary (window, counts, skipped
  activities). The seam is for tests only; the scheduler never sets it.
- **Failure contract**: an unexpected exception (database outage, unknown
  ``engine_trimp_sex``) surfaces as a Celery ``FAILURE`` state; there is no
  silent swallow.
- **Eager mode**: tests set ``task_always_eager`` so no broker is needed.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

import app.db.daily_load as cli_module
from app.scheduler import daily_load_tasks
from app.scheduler.celery_app import celery_app
from app.services.daily_load import DailyLoadReport, SkippedActivity

TASK_NAME = "app.scheduler.daily_load_tasks.recompute_daily_load"

DAY = dt.date(2026, 7, 1)


def _report(**overrides: Any) -> DailyLoadReport:
    values: dict[str, Any] = {
        "window_start": DAY,
        "window_end": DAY,
        "engine_version": "test-0.0.0",
        "activities_considered": 1,
        "rows_upserted": 2,
        "per_day_activities": {DAY: 1},
        "per_sport_rows": {"ride": 1, "combined": 1},
        "per_sport_activities": {"ride": 1},
        "methods_used": {"hrTSS": 1},
        "skipped": (SkippedActivity(activity_id=7, sport="ride", reason="no hr"),),
    }
    values.update(overrides)
    return DailyLoadReport(**values)


class _Sentinel:
    """Opaque injected dependency; forwarded untouched."""


def _fake_recompute(
    calls: list[dict[str, Any]], report: DailyLoadReport | None = None
) -> Any:
    """Async stand-in for app.db.daily_load.run_recompute: records the call."""

    async def _run(settings: Any, **kwargs: Any) -> DailyLoadReport:
        calls.append({"settings": settings, **kwargs})
        return report if report is not None else _report()

    return _run


@pytest.fixture
def recompute_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the run_recompute seam with a recording fake."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(daily_load_tasks, "_run_recompute", _fake_recompute(calls))
    return calls


# ---------------------------------------------------------------------------
# Registration and wiring
# ---------------------------------------------------------------------------


def test_task_is_registered_on_the_app() -> None:
    """Importing the task module registers the task under its full name."""
    assert TASK_NAME in celery_app.tasks


def test_task_wraps_the_same_function_the_cli_uses() -> None:
    """The task's default runner IS the CLI module's run_recompute."""
    assert daily_load_tasks._run_recompute is cli_module.run_recompute


# ---------------------------------------------------------------------------
# Task body (eager mode, offline via the session-factory seam)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("eager_celery")
def test_task_returns_json_summary(
    recompute_calls: list[dict[str, Any]],
) -> None:
    """The task returns a JSON-serializable summary of the report."""
    session_factory = _Sentinel()
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 30, "athlete_id": 1, "session_factory": session_factory}
    )

    assert result.state == "SUCCESS"
    summary = result.get()
    json.dumps(summary)  # must be JSON-serializable
    assert summary == {
        "days": 30,
        "athlete_id": 1,
        "window_start": "2026-07-01",
        "window_end": "2026-07-01",
        "engine_version": "test-0.0.0",
        "activities_considered": 1,
        "rows_upserted": 2,
        "per_day_activities": {"2026-07-01": 1},
        "per_sport_rows": {"ride": 1, "combined": 1},
        "methods_used": {"hrTSS": 1},
        "skipped": [{"activity_id": 7, "sport": "ride", "reason": "no hr"}],
    }


@pytest.mark.usefixtures("eager_celery")
def test_task_forwards_seams_and_defaults(
    recompute_calls: list[dict[str, Any]],
) -> None:
    """days/athlete/window/seams reach the runner; window defaults to today UTC."""
    session_factory = _Sentinel()
    daily_load_tasks.recompute_daily_load.run(
        days=14, athlete_id=2, session_factory=session_factory
    )

    assert len(recompute_calls) == 1
    call = recompute_calls[0]
    assert call["days"] == 14
    assert call["athlete_id"] == 2
    assert call["session_factory"] is session_factory
    # No explicit window: the task uses today (UTC), like the CLI default.
    assert call["window_end"] == dt.datetime.now(dt.UTC).date()


@pytest.mark.usefixtures("eager_celery")
def test_task_parses_explicit_window_end(
    recompute_calls: list[dict[str, Any]],
) -> None:
    """A string window_end is converted to a date before the runner sees it."""
    daily_load_tasks.recompute_daily_load.run(
        window_end="2026-07-01", session_factory=_Sentinel()
    )

    assert recompute_calls[0]["window_end"] == DAY


@pytest.mark.usefixtures("eager_celery")
def test_unexpected_exception_fails_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception surfaces as a Celery FAILURE state."""
    async def _boom(settings: Any, **kwargs: Any) -> DailyLoadReport:
        raise RuntimeError("db down")

    monkeypatch.setattr(daily_load_tasks, "_run_recompute", _boom)
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 1, "session_factory": _Sentinel()}
    )

    assert result.state == "FAILURE"
    assert isinstance(result.result, RuntimeError)
