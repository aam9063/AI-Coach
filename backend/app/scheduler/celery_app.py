"""Celery application for the tri-coach scheduler.

The ``worker`` and ``beat`` services in ``infra/docker-compose.yml`` boot
from this module: it wires broker/backend via ``Settings.redis_url`` and
arms the beat schedule (SCH-1/SCH-2, brief §11): the Intervals.icu sync
EVERY 30 MINUTES, the daily-load recompute offset after each sync, and the
morning brief. The remaining §11 jobs (post-activity review, weekly
report) remain for the later ``scheduled-messages`` tasks (SCH-7/SCH-8)
and are deliberately NOT scheduled here yet — in particular the weekly
report has NO beat entry because its task does not exist yet; the entry
arrives with SCH-8 (documented deviation from SCH-1's wording, which
mentions the Sunday report among the schedule tests).

**Schedule (documented choices):**

- ``daily-intervals-sync`` — ``app.scheduler.tasks.sync_intervals``,
  **every 30 minutes** (:00 and :30) in ``Settings.timezone``
  (``Europe/Madrid``). §11 asks for a 30-minute cadence; the entry keeps
  its historical name. Why every 30 minutes is safe and honest: the sync
  is IDEMPOTENT (Feature 2's upsert anchors — re-running creates no
  duplicates, proven by tests) and the Intervals.icu client is PACED
  (``Settings.intervals_min_request_interval_s``), so the only cost of a
  frequent sync is API calls, and the benefit is data at most half an hour
  behind — the coach's answers stay about current data all day, not only
  after the one early-morning sync.
- ``daily-load-recompute`` — ``app.scheduler.daily_load_tasks
  .recompute_daily_load``, **every 30 minutes at :25 and :55** in the same
  timezone: 25 minutes after each sync entry (a single-athlete sync takes
  minutes, not half an hour), so TSS/CTL/ATL/TSB reflect the freshly
  synced data and are ready for the morning conversation and the 07:30
  brief. **Why recompute after EVERY sync and not only when a sync
  actually brought new activities:** the upsert path counts a re-sync of
  an unchanged activity as ``activities_synced`` — it does not report
  "changed" vs "unchanged" — so "actually new" is not observable without
  building a change-detection mechanism the codebase does not have. The
  recompute is idempotent, single-athlete and bounded to the same 30-day
  window as the sync, so running it every half hour is bounded, cheap work
  that guarantees the numbers can never go stale; gating it on a heuristic
  would save little and risk staleness. This keeps the two entries
  INDEPENDENT (see failure behaviour below) instead of coupling them
  through an intra-task dispatch.
- ``morning-brief`` — ``app.scheduler.morning_brief_tasks.morning_brief``,
  daily at **``Settings.morning_brief_time``** (default 07:30, OWNER
  CHOICE) in the same timezone: after the :25 recompute, so the brief
  composes from freshly persisted numbers (SCH-5). Read once at import
  time — a configured-time change needs a beat restart, like any Celery
  schedule change.

**Failure behaviour:** the entries are independent beat entries. A
failing sync (window-level ``ok=False`` summary or a Celery ``FAILURE``)
never suppresses the recompute or the brief — beat fires each entry on its
own cadence, the recompute is idempotent and simply recomputes from
whatever data the last successful sync left, and the brief honestly skips
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

# Window (in days) of the sync job — shared by the beat entry below and
# by the on-demand ``/sync`` command's default runner (SCH-4), so both
# triggers sync the SAME window. Defined here because ``tasks.py`` imports
# this module (task registration), never the other way around.
SYNC_WINDOW_DAYS = 30

# The beat schedule; see the module docstring for the owner-choice
# rationale. Task names must match the registered tasks exactly (pinned by
# tests/scheduler/test_beat_schedule.py). The brief's time comes from
# Settings (OWNER CHOICE, default 07:30) and is read ONCE at import.
_brief_hour, _brief_minute = _parse_daily_time(get_settings().morning_brief_time)
BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    # §11: every 30 minutes (:00 and :30). Idempotent task + paced client;
    # see the module docstring for the cadence rationale.
    "daily-intervals-sync": {
        "task": "app.scheduler.tasks.sync_intervals",
        "schedule": crontab(minute="*/30"),  # :00 and :30, every hour, daily
        "kwargs": {"days": SYNC_WINDOW_DAYS},
    },
    # 25 minutes after each sync entry (a single-athlete sync takes
    # minutes): every-30-minutes recompute, never stale, bounded work.
    "daily-load-recompute": {
        "task": "app.scheduler.daily_load_tasks.recompute_daily_load",
        "schedule": crontab(minute="25,55"),  # :25 and :55, every hour, daily
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
