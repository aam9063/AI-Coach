"""On-demand CLI for the scheduled jobs (SCH-10; §11, §12.10).

The scheduled jobs fire from Celery beat on their Europe/Madrid crontabs,
which makes them slow to verify: nobody waits half a day for a brief. This
CLI is the seam that makes them runnable ON DEMAND, in-process::

    python -m app.scheduler.cli sync
    python -m app.scheduler.cli morning_brief
    python -m app.scheduler.cli post_activity_review
    python -m app.scheduler.cli weekly_report
    python -m app.scheduler.cli readiness_recompute

    python -m app.scheduler.cli --help

``readiness_recompute`` is the fix for the gap the live run recorded: it
runs the readiness computation the beat now schedules (entry
``readiness-recompute``, :28/:58), so the morning brief finds a snapshot
for today instead of skipping forever.

Contract:

- **No duplicated job logic**: every name dispatches to the EXACT
  synchronous entry point the corresponding Celery task body calls
  (``run_sync`` / ``run_morning_brief_sync`` / ``run_post_activity_review_sync``
  / ``run_weekly_report_sync`` / ``run_readiness_recompute_sync`` — the
  same-function-as-task convention), so
  what the CLI runs is what the beat schedule runs. The identity is pinned
  by :mod:`tests.scheduler.test_cli`.
- **Same seams**: the message jobs accept ``settings``/``session_factory``/
  ``twilio_client`` and the sync accepts ``session_factory``/``client`` —
  injectable for tests, built from configuration when omitted (the
  production path). With no flags the CLI uses the same defaults as the
  beat schedule (``--days`` defaults to ``SYNC_WINDOW_DAYS``, the sync
  window; ``--athlete-id`` defaults to the single-athlete convention).
- **Output**: the job's JSON-serializable summary on stdout (what the
  Celery result would carry), exit code 0. An unknown job name is
  rejected by argparse with exit code 2 and the valid choices on stderr;
  :func:`run_job` enforces the same rule for programmatic callers via
  :class:`UnknownJobError`. Unexpected failures (database outage, empty
  allowlist) propagate — the same failure contract as the tasks.

The CLI sends what the job would send: outside the 24 h free-form window
with no template SID configured the message jobs SKIP loudly (log + audit
row), exactly as scheduled — it is not a dry-run mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.scheduler.celery_app import SYNC_WINDOW_DAYS
from app.scheduler.morning_brief import run_morning_brief_sync
from app.scheduler.post_activity_review import run_post_activity_review_sync
from app.scheduler.readiness_tasks import run_readiness_recompute_sync
from app.scheduler.tasks import run_sync
from app.scheduler.weekly_report import run_weekly_report_sync

__all__ = [
    "JOB_NAMES",
    "SYNC_WINDOW_DAYS",
    "UnknownJobError",
    "build_parser",
    "main",
    "run_job",
    # Re-exported so tests (and callers) can pin the dispatch identity.
    "run_morning_brief_sync",
    "run_post_activity_review_sync",
    "run_readiness_recompute_sync",
    "run_sync",
    "run_weekly_report_sync",
]

#: The four scheduled jobs of §11, in the order the brief lists them, plus
#: the readiness recompute — the fix for the gap the live run recorded:
#: without it nothing computed ``readiness_snapshot`` and the morning
#: brief could never send. Same-seam rule: it dispatches to the exact
#: function the Celery task body calls.
JOB_NAMES: tuple[str, ...] = (
    "sync",
    "morning_brief",
    "post_activity_review",
    "weekly_report",
    "readiness_recompute",
)


class UnknownJobError(ValueError):
    """Raised when a job name is not one of :data:`JOB_NAMES`."""


def run_job(
    name: str,
    *,
    athlete_id: int = 1,
    days: int = SYNC_WINDOW_DAYS,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    twilio_client: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run one scheduled job in-process and return its task summary.

    Dispatches to the same entry point the Celery task body calls; the
    keyword arguments are the jobs' injectable seams (all optional — with
    everything ``None`` the job builds its own engine/client from
    configuration, the production path).
    """
    if name == "sync":
        return run_sync(
            days,
            None,
            session_factory=session_factory,
            client=client,
            settings=settings,
        )
    if name == "morning_brief":
        return run_morning_brief_sync(
            settings=settings,
            session_factory=session_factory,
            twilio_client=twilio_client,
            athlete_id=athlete_id,
        )
    if name == "post_activity_review":
        return run_post_activity_review_sync(
            settings=settings,
            session_factory=session_factory,
            twilio_client=twilio_client,
            athlete_id=athlete_id,
        )
    if name == "weekly_report":
        return run_weekly_report_sync(
            settings=settings,
            session_factory=session_factory,
            twilio_client=twilio_client,
            athlete_id=athlete_id,
        )
    if name == "readiness_recompute":
        return run_readiness_recompute_sync(
            settings=settings,
            session_factory=session_factory,
            athlete_id=athlete_id,
            days=days,
        )
    raise UnknownJobError(
        f"unknown job {name!r}; valid jobs: {', '.join(JOB_NAMES)}"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser (separate so tests can pin --help)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.cli",
        description=(
            "Run one of the four scheduled jobs (§11) once, in-process — "
            "the exact function the Celery task body calls, so the output "
            "matches what the beat schedule would produce now."
        ),
        epilog=(
            "Message jobs respect the §9.1 rule: outside the 24 h free-form "
            "window with no template SID configured they SKIP loudly (log + "
            "audit row) instead of sending."
        ),
    )
    parser.add_argument(
        "job",
        choices=JOB_NAMES,
        help="which scheduled job to run once",
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=1,
        help="athlete id (single-athlete convention; default: 1)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=SYNC_WINDOW_DAYS,
        help=(
            "sync and readiness_recompute jobs only: how many trailing days "
            f"to process (default: {SYNC_WINDOW_DAYS}, the scheduled windows)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse, run the job, print its JSON summary.

    Returns 0 on success; argparse exits 2 for an unknown job name.
    """
    args = build_parser().parse_args(argv)
    summary = run_job(args.job, athlete_id=args.athlete_id, days=args.days)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
