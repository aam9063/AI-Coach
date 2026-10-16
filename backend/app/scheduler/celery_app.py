"""Celery application for the tri-coach scheduler.

The ``worker`` and ``beat`` services in ``infra/docker-compose.yml`` boot
from this module: it wires broker/backend via ``Settings.redis_url`` and
arms the OWNER-APPROVED beat schedule (2026-10-08, extended by SCH-5):
daily Intervals.icu sync, the daily-load recompute, and the morning brief.
The remaining §11 jobs (post-activity review, weekly report, the 30-minute
sync cadence) remain for the later ``scheduled-messages`` tasks (SCH-1/2)
and are deliberately NOT scheduled here yet.

**Schedule (OWNER CHOICES, documented):**

- ``daily-intervals-sync`` — ``app.scheduler.tasks.sync_intervals``, daily
  at **06:30** in ``Settings.timezone`` (``Europe/Madrid``). Morning on
  purpose: the coach talks to the owner in the morning, so the sync has to
  land BEFORE that conversation for its answers to be about current data.
- ``daily-load-recompute`` — ``app.scheduler.daily_load_tasks
  .recompute_daily_load``, daily at **06:55** in the same timezone:
  strictly after the sync window (a single-athlete sync takes minutes, not
  half an hour), so TSS/CTL/ATL/TSB reflect the freshly synced data and are
  ready for the same morning conversation.
- ``morning-brief`` — ``app.scheduler.morning_brief_tasks.morning_brief``,
  daily at **``Settings.morning_brief_time``** (default 07:30, OWNER
  CHOICE) in the same timezone: strictly after the recompute, so the brief
  composes from freshly persisted numbers (SCH-5). Read once at import
  time — a configured-time change needs a beat restart, like any Celery
  schedule change.

**Failure behaviour:** the entries are independent beat entries. A
failing sync (window-level ``ok=False`` summary or a Celery ``FAILURE``)
never suppresses the recompute or the brief — beat fires each entry on its
own cadence, the recompute is idempotent, and the brief honestly skips
(nothing sent, reason logged and audited) when its data or its template is
missing. Nothing is silently skipped either way: the sync reports failed
windows in its summary, the recompute reports every skipped activity, and
the brief reports every skip with its reason.

The Compose beat command uses ``--schedule=/tmp/...`` (read-only ``/app``),
so the schedule lives in the app config below, not in a schedule file.
"""

from typing import Any

from celery import Celery
from celery.schedules import crontab

from app.core.settings import get_settings

# Explicit include (ING-8): the Compose worker command loads this module
# directly, so the task module is imported via `include` rather than
# `autodiscover_tasks` — verifiable with `celery -A ... inspect registered`.
# ``whatsapp_tasks`` is the WhatsApp inbound-message hand-off target (§9.1,
# ODD task WA-2): the webhook only validates and dispatches.
# ``daily_load_tasks`` is the beat-scheduled daily-load recompute (above).
# ``morning_brief_tasks`` is the beat-scheduled morning brief (SCH-5/SCH-6).
TASK_MODULES = [
    "app.scheduler.tasks",
    "app.scheduler.whatsapp_tasks",
    "app.scheduler.daily_load_tasks",
    "app.scheduler.morning_brief_tasks",
]


def _parse_daily_time(value: str) -> tuple[int, int]:
    """Parse an "HH:MM" 24 h clock time; fail loudly on anything else.

    A malformed configured time must never silently fall back to UTC or to
    a default hour — the brief would fire at the wrong moment with zero
    signal.
    """
    try:
        hour_text, minute_text = value.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise ValueError(f"invalid daily time {value!r}: expected 'HH:MM'") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid daily time {value!r}: hour 0-23, minute 0-59")
    return hour, minute

# The beat schedule; see the module docstring for the owner-choice
# rationale. Task names must match the registered tasks exactly (pinned by
# tests/scheduler/test_beat_schedule.py). The brief's time comes from
# Settings (OWNER CHOICE, default 07:30) and is read ONCE at import.
_brief_hour, _brief_minute = _parse_daily_time(get_settings().morning_brief_time)
BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    "daily-intervals-sync": {
        "task": "app.scheduler.tasks.sync_intervals",
        "schedule": crontab(minute=30, hour=6),  # 06:30 owner-local, daily
        "kwargs": {"days": 30},
    },
    "daily-load-recompute": {
        "task": "app.scheduler.daily_load_tasks.recompute_daily_load",
        "schedule": crontab(minute=55, hour=6),  # 06:55 owner-local, daily
        "kwargs": {"days": 30, "athlete_id": 1},
    },
    "morning-brief": {
        "task": "app.scheduler.morning_brief_tasks.morning_brief",
        "schedule": crontab(minute=_brief_minute, hour=_brief_hour),
        "kwargs": {"athlete_id": 1},
    },
}


def create_celery_app() -> Celery:
    """Create the tri-coach Celery app wired to Redis, with the beat schedule."""
    settings = get_settings()
    # Tolerant of extra .env entries, loud about typos (app.core.env_audit):
    # the worker and beat read the same files the API does.
    from app.core.env_audit import warn_unknown_env_keys

    warn_unknown_env_keys()
    app = Celery(
        "tri-coach",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=TASK_MODULES,
    )
    # Crontab entries are evaluated in the owner's timezone (§14 owner
    # choice, ``Europe/Madrid``) — never silently in UTC.
    app.conf.timezone = settings.timezone
    app.conf.enable_utc = True
    app.conf.beat_schedule = BEAT_SCHEDULE
    return app


celery_app = create_celery_app()
