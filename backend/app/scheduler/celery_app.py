"""Celery application for the tri-coach scheduler.

The ``worker`` and ``beat`` services in ``infra/docker-compose.yml`` boot
from this module: it wires broker/backend via ``Settings.redis_url`` and
arms the minimal OWNER-APPROVED beat schedule (2026-10-08) — a daily
Intervals.icu sync followed by the daily-load recompute. The richer beat
jobs (morning brief, post-activity review, weekly report) remain for the
later ``scheduled-messages`` feature (§11) and are deliberately NOT
scheduled here yet.

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

**Failure behaviour:** the two entries are independent beat entries. A
failing sync (window-level ``ok=False`` summary or a Celery ``FAILURE``)
never suppresses the recompute — beat fires each entry on its own cadence,
the recompute is idempotent, and it simply recomputes from whatever data
the last successful sync left. Nothing is silently skipped either way: the
sync reports failed windows in its summary and the recompute reports every
skipped activity.

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
TASK_MODULES = [
    "app.scheduler.tasks",
    "app.scheduler.whatsapp_tasks",
    "app.scheduler.daily_load_tasks",
]

# The minimal beat schedule; see the module docstring for the owner-choice
# rationale. Task names must match the registered tasks exactly (pinned by
# tests/scheduler/test_beat_schedule.py).
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
}


def create_celery_app() -> Celery:
    """Create the tri-coach Celery app wired to Redis, with the beat schedule."""
    settings = get_settings()
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
