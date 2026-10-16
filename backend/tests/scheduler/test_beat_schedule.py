"""Minimal beat schedule (owner-approved, 2026-10-08): daily sync + recompute.

These tests are the executable specification of the schedule armed on the
tri-coach Celery app in :mod:`app.scheduler.celery_app`:

- **Exactly three daily entries** — ``daily-intervals-sync`` (Intervals.icu
  sync), ``daily-load-recompute`` (TSS/CTL/ATL/TSB) and ``morning-brief``
  (SCH-5). The remaining beat jobs (post-activity review, weekly report)
  remain a later feature and must NOT appear here yet.
- **Task names are pinned and resolvable**: each entry's task name must be
  registered on the app (``celery_app.tasks``), so the worker can actually
  run what beat sends — no typos.
- **Cadence**: both entries fire every day in the early morning (sync
  06:30, recompute 06:55) so the data is fresh before the coach talks to
  the owner in the morning.
- **Timezone**: the app timezone is the owner setting (``Settings.timezone``,
  ``Europe/Madrid``) — the schedule must not silently fall back to UTC.
- **Independence / failure behaviour**: the two entries are independent
  beat entries. A failing sync never suppresses the recompute entry: beat
  fires each entry on its own cadence, the recompute is idempotent, and it
  simply recomputes from whatever data the last successful sync left.
- **Offline**: only the in-process Celery config is inspected; no broker,
  no network, no database.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.scheduler.daily_load_tasks
import app.scheduler.morning_brief_tasks
import app.scheduler.tasks
from app.core.settings import get_settings
from app.scheduler.celery_app import BEAT_SCHEDULE, TASK_MODULES, celery_app

SYNC_ENTRY = "daily-intervals-sync"
RECOMPUTE_ENTRY = "daily-load-recompute"
BRIEF_ENTRY = "morning-brief"
SYNC_TASK = "app.scheduler.tasks.sync_intervals"
RECOMPUTE_TASK = "app.scheduler.daily_load_tasks.recompute_daily_load"
BRIEF_TASK = "app.scheduler.morning_brief_tasks.morning_brief"


def _entry(name: str) -> dict[str, Any]:
    return dict(BEAT_SCHEDULE[name])


def test_schedule_has_exactly_the_three_daily_entries() -> None:
    """Sync + recompute + morning brief; nothing else (yet)."""
    assert set(BEAT_SCHEDULE) == {SYNC_ENTRY, RECOMPUTE_ENTRY, BRIEF_ENTRY}


def test_both_entries_pin_registered_task_names() -> None:
    """Each entry targets a task the worker can actually run (no typos)."""
    assert _entry(SYNC_ENTRY)["task"] == SYNC_TASK
    assert _entry(RECOMPUTE_ENTRY)["task"] == RECOMPUTE_TASK
    assert SYNC_TASK in celery_app.tasks
    assert RECOMPUTE_TASK in celery_app.tasks
    # The registered task objects carry exactly the pinned names.
    assert app.scheduler.tasks.sync_intervals.name == SYNC_TASK
    assert app.scheduler.daily_load_tasks.recompute_daily_load.name == RECOMPUTE_TASK
    # The brief entry targets its registered task too.
    assert _entry(BRIEF_ENTRY)["task"] == BRIEF_TASK
    assert BRIEF_TASK in celery_app.tasks
    assert app.scheduler.morning_brief_tasks.morning_brief.name == BRIEF_TASK
    # The worker discovers every task module through the explicit include.
    assert "app.scheduler.tasks" in TASK_MODULES
    assert "app.scheduler.daily_load_tasks" in TASK_MODULES
    assert "app.scheduler.morning_brief_tasks" in TASK_MODULES


def test_sync_fires_daily_at_0630_and_recompute_at_0655() -> None:
    """Daily cadence, recompute strictly after the sync window."""
    sync_cron = _entry(SYNC_ENTRY)["schedule"]
    recompute_cron = _entry(RECOMPUTE_ENTRY)["schedule"]
    assert set(sync_cron.minute) == {30}
    assert set(sync_cron.hour) == {6}
    assert set(recompute_cron.minute) == {55}
    assert set(recompute_cron.hour) == {6}
    # Every day of the week / month: no day-of-week or day-of-month filter.
    assert set(sync_cron.day_of_week) == set(range(7))
    assert set(recompute_cron.day_of_week) == set(range(7))


def test_timezone_is_the_owner_setting_not_utc() -> None:
    """The crontabs are evaluated in the owner's timezone (§14)."""
    assert celery_app.conf.timezone == get_settings().timezone
    assert celery_app.conf.timezone == "Europe/Madrid"


def test_sync_entry_uses_the_default_30_day_window() -> None:
    """The sync entry passes the task's documented default window."""
    assert _entry(SYNC_ENTRY)["kwargs"] == {"days": 30}


def test_recompute_entry_pins_window_and_athlete() -> None:
    """The recompute entry pins its window length and athlete id."""
    assert _entry(RECOMPUTE_ENTRY)["kwargs"] == {"days": 30, "athlete_id": 1}


def test_morning_brief_fires_daily_at_the_configured_time() -> None:
    """SCH-5: daily, at the OWNER-CONFIGURED Europe/Madrid time (§11).

    The default (``Settings.morning_brief_time``, 07:30) lands strictly
    after the 06:55 daily-load recompute so the brief reads freshly
    recomputed numbers.
    """
    brief_cron = _entry(BRIEF_ENTRY)["schedule"]
    expected_hour, expected_minute = map(
        int, get_settings().morning_brief_time.split(":")
    )
    assert set(brief_cron.minute) == {expected_minute}
    assert set(brief_cron.hour) == {expected_hour}
    # Every day of the week / month: no day-of-week or day-of-month filter.
    assert set(brief_cron.day_of_week) == set(range(7))


def test_morning_brief_time_is_parsed_strictly() -> None:
    """A malformed configured time fails loudly, never silently at UTC."""
    from app.scheduler.celery_app import _parse_daily_time

    assert _parse_daily_time("07:30") == (7, 30)
    with pytest.raises(ValueError):
        _parse_daily_time("7am")
    with pytest.raises(ValueError):
        _parse_daily_time("24:00")
