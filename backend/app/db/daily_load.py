"""CLI: recompute and persist ``daily_load`` for a trailing window (LOAD-10).

Usage::

    python -m app.db.daily_load --days N [--athlete-id ID] [--end YYYY-MM-DD]

Reads the athlete thresholds from settings (§14 owner configuration; a
missing value stays ``None`` and the corresponding load method is skipped
and reported, never defaulted), derives the engine inputs from the stored
``activity``/``activity_stream`` rows via
:func:`app.services.daily_load.recompute_daily_load`, upserts the
``daily_load`` rows (one commit at the end) and prints a report: per-day
and per-sport activity counts, persisted row counts per sport, and every
skipped activity with its reason.

TRIMP coefficients: the documented male Banister 1991 set (0.64/1.92) is
used as the default — settings has no sex field yet; a configurable sex /
coefficient set lands with LOAD-11's configurable-constants work.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence

from app.core.settings import Settings, get_settings
from app.db.session import create_db_engine, make_session_factory
from app.engine.load import ThresholdBundle, TrimpCoefficients
from app.services.daily_load import DailyLoadReport, recompute_daily_load

__all__ = ["build_parser", "format_report", "main", "thresholds_from_settings"]


def thresholds_from_settings(settings: Settings) -> ThresholdBundle:
    """Map the settings' athlete thresholds onto the engine's bundle (§14).

    Missing values stay ``None`` — the engine then reports the affected
    method as skipped instead of silently defaulting (§5.1). The sRPE
    TSS-equivalent factor keeps the engine default 1.0 (raw Foster load);
    a calibrated, configurable factor is LOAD-11 scope.
    """
    return ThresholdBundle(
        ftp_watts=settings.athlete_ftp_w,
        threshold_run_speed_mps=settings.athlete_threshold_run_speed_mps,
        css_mps=settings.athlete_css_speed_mps,
        lthr_bpm=settings.athlete_lthr_bpm,
        hr_max_bpm=settings.athlete_hr_max_bpm,
        hr_rest_bpm=settings.athlete_hr_rest_bpm,
    )


def format_report(report: DailyLoadReport) -> str:
    """Human-readable multi-line report of one recompute run."""
    lines = [
        f"daily_load recompute: window {report.window_start}..{report.window_end} "
        f"(engine_version {report.engine_version})",
        f"activities considered: {report.activities_considered}",
    ]
    if report.per_day_activities:
        lines.append("per-day activity counts:")
        lines.extend(
            f"  {day}: {count}" for day, count in report.per_day_activities.items()
        )
    else:
        lines.append("per-day activity counts: none")
    if report.per_sport_activities:
        lines.append("activities contributing load, per sport:")
        lines.extend(
            f"  {sport}: {count}"
            for sport, count in report.per_sport_activities.items()
        )
    if report.per_sport_rows:
        lines.append("persisted rows, per sport:")
        lines.extend(
            f"  {sport}: {count} row(s)" for sport, count in report.per_sport_rows.items()
        )
    lines.append(f"rows upserted: {report.rows_upserted}")
    if report.skipped:
        lines.append(f"skipped activities ({len(report.skipped)}) — never silent:")
        lines.extend(
            f"  activity {skip.activity_id} ({skip.sport}): {skip.reason}"
            for skip in report.skipped
        )
    else:
        lines.append("skipped activities: none")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the daily-load CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.db.daily_load",
        description=(
            "Recompute the deterministic engine's daily load (TSS/CTL/ATL/TSB) "
            "from the stored activities and streams and upsert the daily_load "
            "rows for a trailing window of days. Prints per-day and per-sport "
            "counts plus every skipped activity with its reason."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="length of the trailing window in calendar days (default: 30)",
    )
    parser.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="last day of the window (default: today, UTC)",
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=1,
        metavar="ID",
        help="athlete id to persist rows for (default: 1)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns 0 on success, 1 on configuration errors."""
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.days <= 0:
        print(f"--days must be positive, got {args.days}", file=sys.stderr)
        return 1
    if args.end is None:
        window_end = dt.datetime.now(dt.UTC).date()
    else:
        try:
            window_end = dt.date.fromisoformat(args.end)
        except ValueError:
            print(f"--end must be YYYY-MM-DD, got {args.end!r}", file=sys.stderr)
            return 1

    thresholds = thresholds_from_settings(settings)
    coefficients = TrimpCoefficients.from_sex("male")  # documented default; see module docstring

    async def _run() -> DailyLoadReport:
        engine = create_db_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                report = await recompute_daily_load(
                    session,
                    athlete_id=args.athlete_id,
                    window_end=window_end,
                    days=args.days,
                    thresholds=thresholds,
                    coefficients=coefficients,
                    engine_version=settings.engine_version,
                )
                await session.commit()
                return report
        finally:
            await engine.dispose()

    report = asyncio.run(_run())
    print(format_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
