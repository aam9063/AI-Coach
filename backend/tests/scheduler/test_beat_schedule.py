"""Beat schedule (ODD task SCH-1/SCH-2, brief §11): 30-minute sync + recompute.

These tests are the executable specification of the schedule armed on the
tri-coach Celery app in :mod:`app.scheduler.celery_app`:

- **Exactly five entries** — ``daily-intervals-sync`` (Intervals.icu sync,
  EVERY 30 MINUTES per §11), ``daily-load-recompute`` (TSS/CTL/ATL/TSB,
  also every 30 minutes, offset after the sync), ``morning-brief``
  (SCH-5, daily at the configured time), ``post-activity-review`` (SCH-7,
  the idempotent review pass at :05/:35 after each sync entry) and
  ``weekly-report`` (SCH-8, Sundays at the configured time). The earlier
  SCH-1 deviation — no beat entry for a task that does not exist — is
  lifted now that both tasks exist (SCH-7/SCH-8).
- **Task names are pinned and resolvable**: each entry's task name must be
  registered on the app (``celery_app.tasks``), so the worker can actually
  run what beat sends — no typos.
- **Cadence**: the sync fires every 30 minutes (§11: ``sync_intervals``
  every 30 min; the task is idempotent and the client is paced, so a
  re-run creates no duplicates and costs only API calls), the recompute
  fires every 30 minutes 25 minutes after each sync, the review pass
  fires at :05/:35 after each sync entry (its trigger is documented in
  the schedule module), and the report fires on Sundays.
- **Timezone**: the app timezone is the owner setting (``Settings.timezone``,
  ``Europe/Madrid``) — the schedule must not silently fall back to UTC.
- **Independence / failure behaviour**: the three entries are independent
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
import app.scheduler.post_activity_review_tasks
import app.scheduler.tasks
import app.scheduler.weekly_report_tasks
from app.core.settings import get_settings
from app.scheduler.celery_app import BEAT_SCHEDULE, TASK_MODULES, celery_app

SYNC_ENTRY = "daily-intervals-sync"
RECOMPUTE_ENTRY = "daily-load-recompute"
BRIEF_ENTRY = "morning-brief"
REVIEW_ENTRY = "post-activity-review"
WEEKLY_ENTRY = "weekly-report"
SYNC_TASK = "app.scheduler.tasks.sync_intervals"
RECOMPUTE_TASK = "app.scheduler.daily_load_tasks.recompute_daily_load"
BRIEF_TASK = "app.scheduler.morning_brief_tasks.morning_brief"
REVIEW_TASK = "app.scheduler.post_activity_review_tasks.post_activity_review"
WEEKLY_TASK = "app.scheduler.weekly_report_tasks.weekly_report"


def _entry(name: str) -> dict[str, Any]:
    return dict(BEAT_SCHEDULE[name])


def test_schedule_has_exactly_the_five_entries() -> None:
    """Sync + recompute + brief + review pass + Sunday report (SCH-7/8)."""
    assert set(BEAT_SCHEDULE) == {
        SYNC_ENTRY,
        RECOMPUTE_ENTRY,
        BRIEF_ENTRY,
        REVIEW_ENTRY,
        WEEKLY_ENTRY,
    }


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
    # And so do the review pass and the weekly report (SCH-7/SCH-8).
    assert _entry(REVIEW_ENTRY)["task"] == REVIEW_TASK
    assert REVIEW_TASK in celery_app.tasks
    assert (
        app.scheduler.post_activity_review_tasks.post_activity_review.name
        == REVIEW_TASK
    )
    assert _entry(WEEKLY_ENTRY)["task"] == WEEKLY_TASK
    assert WEEKLY_TASK in celery_app.tasks
    assert app.scheduler.weekly_report_tasks.weekly_report.name == WEEKLY_TASK
    # The worker discovers every task module through the explicit include.
    assert "app.scheduler.tasks" in TASK_MODULES
    assert "app.scheduler.daily_load_tasks" in TASK_MODULES
    assert "app.scheduler.morning_brief_tasks" in TASK_MODULES
    assert "app.scheduler.post_activity_review_tasks" in TASK_MODULES
    assert "app.scheduler.weekly_report_tasks" in TASK_MODULES


def test_sync_fires_every_30_minutes_and_recompute_offset_after() -> None:
    """§11: sync every 30 minutes; recompute 25 minutes after each sync.

    The 25-minute offset gives a single-athlete sync (minutes, not half an
    hour) room to finish before the recompute reads the data, mirroring the
    former 06:30→06:55 daily gap. Both fire every hour of every day.
    """
    sync_cron = _entry(SYNC_ENTRY)["schedule"]
    recompute_cron = _entry(RECOMPUTE_ENTRY)["schedule"]
    assert set(sync_cron.minute) == {0, 30}
    assert set(sync_cron.hour) == set(range(24))
    assert set(recompute_cron.minute) == {25, 55}
    assert set(recompute_cron.hour) == set(range(24))
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
    after the :25 half-hour recompute (07:25) so the brief reads freshly
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


def test_post_activity_review_pass_fires_after_each_sync_entry() -> None:
    """SCH-7: the idempotent review pass at :05/:35 — after each sync
    entry (:00/:30), before the following one. Its trigger (why a pass
    and not an ingestion event) is documented in the schedule module; a
    re-run reviews nothing, so this cadence cannot spam."""
    review_cron = _entry(REVIEW_ENTRY)["schedule"]
    assert set(review_cron.minute) == {5, 35}
    assert set(review_cron.hour) == set(range(24))
    assert set(review_cron.day_of_week) == set(range(7))
    assert _entry(REVIEW_ENTRY)["kwargs"] == {"athlete_id": 1}


def test_weekly_report_fires_on_sundays_at_the_configured_time() -> None:
    """SCH-8: Sundays (celery's day_of_week 0 = Sunday), at the
    OWNER-CONFIGURED Europe/Madrid time (``Settings.weekly_report_time``,
    default 18:00)."""
    weekly_cron = _entry(WEEKLY_ENTRY)["schedule"]
    expected_hour, expected_minute = map(
        int, get_settings().weekly_report_time.split(":")
    )
    assert set(weekly_cron.minute) == {expected_minute}
    assert set(weekly_cron.hour) == {expected_hour}
    # Sunday only — celery normalizes the day name to its number (0).
    assert set(weekly_cron.day_of_week) == {0}
