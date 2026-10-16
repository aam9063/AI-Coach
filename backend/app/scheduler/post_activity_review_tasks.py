"""Celery task: the post-activity review (SCH-7, §11).

The trigger, defined honestly (see :mod:`app.scheduler.post_activity_review`
for the full reasoning): the sync cannot tell a NEW activity from a
re-synced one (the upsert path counts both as ``activities_synced``), so
"triggered when a new activity is ingested" is implemented as an
IDEMPOTENT REVIEW PASS armed on the beat AFTER each sync+recompute cycle
(:05 and :35, Europe/Madrid) — see :mod:`app.scheduler.celery_app`. The
``message_log`` review marker makes the pass spam-proof by construction:
an activity is reviewed EXACTLY once, whatever the pass's cadence.

The task body is :func:`app.scheduler.post_activity_review
.run_post_activity_review_sync` — the exact orchestrator the tests
exercise (same-function-as-task, the LOAD-10/WA-3 convention). It never
raises for a *content* skip (no template configured, nothing to review);
unexpected exceptions (database outage, empty allowlist) propagate and
surface as a Celery ``FAILURE`` state — never silently swallowed.

Offline tests: inject ``settings``/``session_factory``/``twilio_client``
(test-only seams; the beat schedule never sets them).
"""

from __future__ import annotations

from typing import Any

from app.scheduler.celery_app import celery_app
from app.scheduler.post_activity_review import run_post_activity_review_sync

__all__ = ["post_activity_review"]


@celery_app.task(name="app.scheduler.post_activity_review_tasks.post_activity_review")  # type: ignore[untyped-decorator]
def post_activity_review(athlete_id: int = 1) -> dict[str, Any]:
    """Celery task: review every not-yet-reviewed recent activity.

    Returns a JSON-serializable summary (``reviewed``/``sent``/``skipped``
    /``details``). The ``athlete_id`` keyword is the single-athlete
    convention (``daily_load_tasks``); never set by the beat schedule.
    """
    return run_post_activity_review_sync(athlete_id=athlete_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("run via celery worker/beat, not directly")
