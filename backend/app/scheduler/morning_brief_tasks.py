"""Celery task: the daily morning brief (SCH-5/SCH-6; §11).

Scheduled daily at the OWNER-CONFIGURED time (``Settings
.morning_brief_time``, default **07:30** Europe/Madrid — strictly after the
06:55 daily-load recompute, so the brief reads freshly recomputed numbers;
see :mod:`app.scheduler.celery_app`).

The task body is :func:`app.scheduler.morning_brief.run_morning_brief_sync`
— the exact orchestrator the tests exercise (same-function-as-task, the
LOAD-10/WA-3 convention). It composes the brief from PERSISTED engine data
(readiness snapshot + daily load), applies the §9.1 free-form window /
template rule, and either sends, uses the configured approved template, or
skips with the reason stated in the log, the task summary and the audit
trail. It never raises for a *content* skip (nothing to brief, no
template); unexpected exceptions (database outage, empty allowlist)
propagate and surface as a Celery ``FAILURE`` state — never silently
swallowed.

Offline tests: inject ``settings``/``session_factory``/``twilio_client``
(test-only seams; the beat schedule never sets them).
"""

from __future__ import annotations

from typing import Any

from app.scheduler.celery_app import celery_app
from app.scheduler.morning_brief import run_morning_brief_sync

__all__ = ["morning_brief"]


@celery_app.task(name="app.scheduler.morning_brief_tasks.morning_brief")  # type: ignore[untyped-decorator]
def morning_brief(athlete_id: int = 1) -> dict[str, Any]:
    """Celery task: compose and deliver (or honestly skip) the morning brief.

    Returns a JSON-serializable summary (``mode``/``sent``/``reason``/
    ``to``/``brief``). The ``athlete_id`` keyword is the single-athlete
    convention (``daily_load_tasks``); never set by the beat schedule.
    """
    return run_morning_brief_sync(athlete_id=athlete_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("run via celery worker/beat, not directly")
