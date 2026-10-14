"""Minimal beat schedule (owner-approved, 2026-10-08): daily sync + recompute.

These tests are the executable specification of the schedule armed on the
tri-coach Celery app in :mod:`app.scheduler.celery_app`:

- **Exactly two daily entries** — ``daily-intervals-sync`` (Intervals.icu
  sync) and ``daily-load-recompute`` (TSS/CTL/ATL/TSB). The richer beat
  jobs (morning brief, post-activity review, weekly report) remain a later
  feature and must NOT appear here yet.
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

import app.scheduler.daily_load_tasks
import app.scheduler.tasks
from app.core.settings import get_settings
from app.scheduler.celery_app import BEAT_SCHEDULE, TASK_MODULES, celery_app

SYNC_ENTRY = "daily-intervals-sync"
RECOMPUTE_ENTRY = "daily-load-recompute"
SYNC_TASK = "app.scheduler.tasks.sync_intervals"
RECOMPUTE_TASK = "app.scheduler.daily_load_tasks.recompute_daily_load"


def _entry(name: str) -> dict[str, Any]:
    return dict(BEAT_SCHEDULE[name])


def test_schedule_has_exactly_the_two_daily_entries() -> None:
    """The minimal schedule: sync + recompute, nothing else (yet)."""
    assert set(BEAT_SCHEDULE) == {SYNC_ENTRY, RECOMPUTE_ENTRY}


def test_both_entries_pin_registered_task_names() -> None:
    """Each entry targets a task the worker can actually run (no typos)."""
    assert _entry(SYNC_ENTRY)["task"] == SYNC_TASK
    assert _entry(RECOMPUTE_ENTRY)["task"] == RECOMPUTE_TASK
    assert SYNC_TASK in celery_app.tasks
    assert RECOMPUTE_TASK in celery_app.tasks
    # The registered task objects carry exactly the pinned names.
    assert app.scheduler.tasks.sync_intervals.name == SYNC_TASK
    assert app.scheduler.daily_load_tasks.recompute_daily_load.name == RECOMPUTE_TASK
    # The worker discovers both task modules through the explicit include.
    assert "app.scheduler.tasks" in TASK_MODULES
    assert "app.scheduler.daily_load_tasks" in TASK_MODULES


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
