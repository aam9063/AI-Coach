"""Celery task: the Sunday weekly report (SCH-8, §11).

Scheduled SUNDAYS at the OWNER-CONFIGURED time (``Settings
.weekly_report_time``, default **18:00** Europe/Madrid — OWNER CHOICE;
see :mod:`app.scheduler.celery_app`). The task body is :func:
`app.scheduler.weekly_report.run_weekly_report_sync` — the exact
orchestrator the tests exercise (same-function-as-task, the LOAD-10/WA-3
convention). It composes the report from PERSISTED engine data for the
COMPLETED ISO week, surfaces the PENDING threshold proposals for the
owner's review (never applied), applies the §9.1 free-form window /
template rule, and either sends, uses the configured approved template,
or skips with the reason stated in the log and the audit trail. It never
raises for a *content* skip (no data, no template); unexpected exceptions
(database outage, empty allowlist) propagate and surface as a Celery
``FAILURE`` state — never silently swallowed.

Offline tests: inject ``settings``/``session_factory``/``twilio_client``
(test-only seams; the beat schedule never sets them).
"""

from __future__ import annotations

from typing import Any

from app.scheduler.celery_app import celery_app
from app.scheduler.weekly_report import run_weekly_report_sync

__all__ = ["weekly_report"]


@celery_app.task(name="app.scheduler.weekly_report_tasks.weekly_report")  # type: ignore[untyped-decorator]
def weekly_report(athlete_id: int = 1) -> dict[str, Any]:
    """Celery task: compose and deliver (or honestly skip) the weekly report.

    Returns a JSON-serializable summary (``mode``/``sent``/``reason``/
    ``to``/``report``). The ``athlete_id`` keyword is the single-athlete
    convention (``daily_load_tasks``); never set by the beat schedule.
    """
    return run_weekly_report_sync(athlete_id=athlete_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("run via celery worker/beat, not directly")
