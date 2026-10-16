"""Celery application for the tri-coach scheduler.

The ``worker`` and ``beat`` services in ``infra/docker-compose.yml`` boot
from this module: it wires broker/backend via ``Settings.redis_url`` and
arms the beat schedule (SCH-1/SCH-2, brief §11): the Intervals.icu sync
EVERY 30 MINUTES, the daily-load recompute offset after each sync, the
morning brief, the post-activity review (SCH-7) and the Sunday weekly
report (SCH-8). The earlier note that these last two entries were
"deliberately NOT scheduled yet" (documented SCH-1 deviation: no beat
entry for a task that does not exist) is now lifted — both tasks exist.

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
- ``post-activity-review`` — ``app.scheduler.post_activity_review_tasks
  .post_activity_review``, **every 30 minutes at :05 and :35** in the
  same timezone (SCH-7). **The trigger, defined honestly:** §11 says the
  review fires "when a new activity is ingested", but the sync CANNOT
  report newness — the upsert path counts a re-synced (unchanged)
  activity as ``activities_synced``, the same limitation already
  recorded for the recompute cadence above. A trigger keyed on the
  sync's return would be meaningless, and a naïve intra-task dispatch
  would couple two entries the schedule deliberately keeps independent.
  So the review is an IDEMPOTENT PASS: each run reviews the activities
  that started within ``Settings.post_activity_review_lookback_hours``
  (default 48 h, OWNER CHOICE — a first 30-day backfill must not become
  30 proactive messages) and have no review marker yet in the
  ``message_log`` audit trail. The marker (payload ``kind`` +
  ``activity_id``, written with the audit row BEFORE the send) makes an
  activity reviewed EXACTLY ONCE whatever the pass's cadence — a re-run
  reviews nothing, so this trigger cannot spam. The :05/:35 offsets put
  each pass after a sync entry; a brand-new activity whose daily-load
  row is not yet computed is reviewed with that gap stated honestly
  (the RPE request does not depend on the numbers).
- ``weekly-report`` — ``app.scheduler.weekly_report_tasks.weekly_report``,
  **Sundays at ``Settings.weekly_report_time``** (default 18:00, OWNER
  CHOICE) in the same timezone (SCH-8, §11 "Sundays"). It composes from
  persisted engine data for the COMPLETED ISO week and surfaces the
  pending threshold proposals for the owner's review — never applied.

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
# ``readiness_tasks`` is the beat-scheduled readiness recompute — the fix
# for the recorded gap: nothing scheduled the readiness computation, so
# the morning brief could never find a snapshot for today.
# ``morning_brief_tasks`` is the beat-scheduled morning brief (SCH-5/SCH-6).
# ``post_activity_review_tasks`` is the beat-armed post-activity review
# pass (SCH-7). ``weekly_report_tasks`` is the beat-scheduled Sunday
# weekly report (SCH-8).
TASK_MODULES = [
    "app.scheduler.tasks",
    "app.scheduler.whatsapp_tasks",
    "app.scheduler.daily_load_tasks",
    "app.scheduler.readiness_tasks",
    "app.scheduler.morning_brief_tasks",
    "app.scheduler.post_activity_review_tasks",
    "app.scheduler.weekly_report_tasks",
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

# Window (in days) of the readiness recompute — the SAME documented bound
# as the sync and the load recompute (30 trailing days; the service reads
# the signals' lookback series on top of it, so the total read stays
# bounded to weeks, never years). Defined here so both the beat entry and
# the task/CLI defaults import one constant (the SYNC_WINDOW_DAYS rule).
READINESS_WINDOW_DAYS = 30

# The beat schedule; see the module docstring for the owner-choice
# rationale. Task names must match the registered tasks exactly (pinned by
# tests/scheduler/test_beat_schedule.py). The brief's time comes from
# Settings (OWNER CHOICE, default 07:30) and is read ONCE at import.
_brief_hour, _brief_minute = _parse_daily_time(get_settings().morning_brief_time)
_weekly_hour, _weekly_minute = _parse_daily_time(get_settings().weekly_report_time)
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
    # GAP FIX (live run, recorded in the ODD task): nothing scheduled the
    # readiness computation, so ``readiness_snapshot`` had no row for
    # today and the morning brief could never send. This entry computes it
    # through the SAME service seam the tool path uses, at :28/:58 — after
    # the :25/:55 load recompute whose TSB rows readiness reads, before
    # the 07:30 brief that consumes the snapshot. Its OWN entry (not an
    # extension of the load task): a readiness failure surfaces as its own
    # Celery FAILURE instead of being coupled into — or silently swallowed
    # by — the load task. Bounded to READINESS_WINDOW_DAYS; idempotent
    # upserts per (athlete, date); no schema change.
    "readiness-recompute": {
        "task": "app.scheduler.readiness_tasks.recompute_readiness",
        "schedule": crontab(minute="28,58"),  # :28 and :58, every hour, daily
        "kwargs": {"days": READINESS_WINDOW_DAYS, "athlete_id": 1},
    },
    # SCH-7: the idempotent review pass after each sync entry. The
    # trigger's honesty (why a pass and not an ingestion event) is
    # documented in the module docstring above.
    "post-activity-review": {
        "task": "app.scheduler.post_activity_review_tasks.post_activity_review",
        "schedule": crontab(minute="5,35"),
        "kwargs": {"athlete_id": 1},
    },
    # SCH-8: Sundays (celery's day_of_week 0 = Sunday), OWNER-CHOICE time.
    "weekly-report": {
        "task": "app.scheduler.weekly_report_tasks.weekly_report",
        "schedule": crontab(minute=_weekly_minute, hour=_weekly_hour, day_of_week="sunday"),
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
