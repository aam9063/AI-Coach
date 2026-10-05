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

TRIMP coefficients (LOAD-11): selected from settings via
``engine_trimp_sex`` (owner choice, default "male") with the Banister 1991
exponent values ``engine_trimp_male_a/b`` and ``engine_trimp_female_a/b``
overridable — see :func:`trimp_coefficients_from_settings`. All remaining
engine constants (PMC time constants, confidence threshold, NP window,
minimum valid fraction, hrTSS reference duration, sRPE factor) are also
settings-sourced and passed into the pure engine — never re-hardcoded
here.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from typing import Any

from app.core.settings import Settings, get_settings
from app.db.session import create_db_engine, make_session_factory
from app.engine.load import ThresholdBundle, TrimpCoefficients
from app.services.daily_load import DailyLoadReport, recompute_daily_load

__all__ = [
    "build_parser",
    "engine_constants_from_settings",
    "format_report",
    "main",
    "thresholds_from_settings",
    "trimp_coefficients_from_settings",
]

# LOAD-12 note: with an owner-entered RPE stored on strength activities,
# this CLI's report shows the sRPE method under "load methods used" and
# the calibration tool (app.tools.calibrate_srpe) reads the same data to
# replace the placeholder factor. Nothing else changes for other sports.


def thresholds_from_settings(settings: Settings) -> ThresholdBundle:
    """Map the settings' athlete thresholds onto the engine's bundle (§14).

    Missing values stay ``None`` — the engine then reports the affected
    method as skipped instead of silently defaulting (§5.1). The sRPE
    TSS-equivalent factor comes from settings (LOAD-11): its shipped value
    1.0 is a documented PLACEHOLDER — the raw Foster load in arbitrary
    units, NOT calibrated to the TSS scale, because there is no RPE data
    anywhere to calibrate against. Documented calibration method: once gym
    sessions record an RPE, compare their sRPE AU against the hrTSS of the
    same sessions (gym sessions carry HR) and set the factor so the two
    agree on average — measured by the read-only tool
    ``python -m app.tools.calibrate_srpe`` (LOAD-12).
    """
    return ThresholdBundle(
        ftp_watts=settings.athlete_ftp_w,
        threshold_run_speed_mps=settings.athlete_threshold_run_speed_mps,
        css_mps=settings.athlete_css_speed_mps,
        lthr_bpm=settings.athlete_lthr_bpm,
        hr_max_bpm=settings.athlete_hr_max_bpm,
        hr_rest_bpm=settings.athlete_hr_rest_bpm,
        srpe_tss_equivalent_factor=settings.engine_srpe_tss_equivalent_factor,
    )


def trimp_coefficients_from_settings(settings: Settings) -> TrimpCoefficients:
    """Build the TRIMP coefficient set selected by settings (LOAD-11).

    ``engine_trimp_sex`` (owner choice, default ``"male"``) selects the
    Banister 1991 exponent set; the coefficient values themselves are
    overridable via ``engine_trimp_male_a/b`` and
    ``engine_trimp_female_a/b``. An unknown sex raises ``ValueError`` —
    never silently defaulted.
    """
    sex = settings.engine_trimp_sex.strip().lower()
    if sex == "male":
        return TrimpCoefficients(
            a=settings.engine_trimp_male_a, b=settings.engine_trimp_male_b
        )
    if sex == "female":
        return TrimpCoefficients(
            a=settings.engine_trimp_female_a, b=settings.engine_trimp_female_b
        )
    raise ValueError(
        f"unknown engine_trimp_sex {settings.engine_trimp_sex!r}: "
        "expected 'male' or 'female'"
    )


def engine_constants_from_settings(settings: Settings) -> dict[str, Any]:
    """Engine constants passed into :func:`recompute_daily_load` (LOAD-11).

    Pure mapping from the sourced settings fields onto the service's
    keyword parameters (``**``-unpacked at the call site); the service
    defaults are the pure engine's documented fallbacks and are never
    re-hardcoded here. The key names are pinned by the mapping test and
    the service signature accepts exactly these parameters.
    """
    return {
        "tau_ctl_days": settings.engine_tau_ctl_days,
        "tau_atl_days": settings.engine_tau_atl_days,
        "min_history_days": settings.engine_min_history_days,
        "np_window_samples": settings.engine_np_window_samples,
        "np_min_valid_fraction": settings.engine_min_valid_fraction,
        "trimp_reference_minutes": settings.engine_trimp_reference_minutes,
    }


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
    if report.methods_used:
        # LOAD-12: the chosen methods are visible per window, so the new
        # strength-sRPE path (owner-entered RPE) is traceable in the report.
        lines.append("load methods used (chosen method per activity, window total):")
        lines.extend(
            f"  {method}: {count}" for method, count in report.methods_used.items()
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
    coefficients = trimp_coefficients_from_settings(settings)
    engine_constants = engine_constants_from_settings(settings)

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
                    **engine_constants,
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
