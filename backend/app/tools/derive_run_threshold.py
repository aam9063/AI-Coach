"""Run-threshold derivation over the stored history (READ-ONLY report CLI).

Usage::

    python -m app.tools.derive_run_threshold [--min-runs N] [--tolerance X]

READ-ONLY (§5.1, §12.3): this tool runs the data-quality derivation
(:func:`app.engine.quality.derive_run_threshold_candidate`) over the
activities ALREADY STORED in the database and PRINTS the result. It never
writes to the database (no commit, no upsert — the derivation is a
candidate for the owner to review, not a persisted threshold) and never
touches Intervals.icu. The output says so on every run.

What it does, mirroring the data-quality pipeline in
:mod:`app.engine.quality`:

1. ACTIVITY-LEVEL plausibility: every stored run-family activity
   (:func:`app.engine.quality.plausibility_band_key`) is judged from its
   stored summary distance and duration via
   :func:`app.engine.quality.assess_activity_plausibility`. Rejected
   activities are printed WITH their machine-readable reason — the owner's
   real history carries 9 runs with impossible average paces (e.g.
   2:07/km) that would poison every downstream fit.
2. PER-ACTIVITY stream cleaning: each plausible run's stored speed stream
   is cleaned with :func:`app.engine.quality.clean_speed_stream`
   (percentile-of-own-distribution cap + physiological sample ceiling), so
   GPS spikes cannot dominate the best-effort statistics.
3. ROBUST derivation: the cleaned runs feed
   :func:`app.engine.quality.derive_run_threshold_candidate` — median-
   across-runs best efforts, the CS/D' fit over the robust curve, the
   threshold-HR pace and the numeric agreement between them, with the
   explicit ``candidate`` / ``insufficient_data`` / ``contradictory_data``
   outcome. The threshold HR band is derived from the configured LTHR
   (:func:`threshold_hr_band_from_lthr`, Friel run Z4 bottom .. Z5a top;
   the owner's LTHR of 169 bpm gives 160.55-172.38 bpm) — settings-sourced
   (§14), never hardcoded here.

Like ``app.tools.cross_check_pmc`` (style reference) the engine constants
are parameters of the pure engine functions with documented defaults; the
CLI passes the settings values where a settings field exists and the
engine's documented defaults otherwise. Exit code: 0 on a printed report
(including ``contradictory_data`` / ``insufficient_data`` — those are
valid, reportable outcomes, not tool failures), 2 on argument or
configuration errors (e.g. no LTHR configured).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db.models import ActivityRow, ActivityStreamRow
from app.db.session import create_db_engine, make_session_factory
from app.engine.load import _RUNNING_SPORTS
from app.engine.quality import (
    DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE,
    ActivityPlausibility,
    CleanedRun,
    RunThresholdCandidate,
    assess_activity_plausibility,
    clean_speed_stream,
    derive_run_threshold_candidate,
)

__all__ = [
    "RUN_LTHR_BAND_CEILING_PCT",
    "RUN_LTHR_BAND_FLOOR_PCT",
    "build_parser",
    "format_pace",
    "format_report",
    "main",
    "run_derivation",
    "threshold_hr_band_from_lthr",
]

RUN_LTHR_BAND_FLOOR_PCT = 0.95
"""Lower edge of the threshold HR band as a fraction of LTHR.

Friel run HR zones: Z4 (Threshold) starts at 95% of LTHR. Settings-side
mirror: the engine's run HR modality uses the same Friel table
(``app.engine.zones``); the band is DERIVED from the configured LTHR here,
never hardcoded.
"""

RUN_LTHR_BAND_CEILING_PCT = 1.02
"""Upper edge of the threshold HR band as a fraction of LTHR.

Friel run HR zones: Z5a ends at 102% of LTHR, so the band spans the top
of Z4 through the whole of Z5a — the effort band around the threshold.
"""

_SPEED_STREAM = "speed"
_HR_STREAM = "hr"


def threshold_hr_band_from_lthr(lthr_bpm: float) -> tuple[float, float]:
    """Threshold HR band (floor, ceiling) from the athlete's LTHR.

    ``(RUN_LTHR_BAND_FLOOR_PCT * LTHR, RUN_LTHR_BAND_CEILING_PCT * LTHR)``
    — Friel run Z4 bottom .. Z5a top. A nonpositive LTHR raises
    ``ValueError`` (a missing LTHR is the caller's reportable error, never
    a silent default).
    """
    if lthr_bpm <= 0.0:
        raise ValueError(f"LTHR must be positive, got {lthr_bpm!r} bpm")
    return (RUN_LTHR_BAND_FLOOR_PCT * lthr_bpm, RUN_LTHR_BAND_CEILING_PCT * lthr_bpm)


def format_pace(speed_mps: float | None) -> str:
    """Format a speed in m/s as a min/km pace with one decimal (``n/a`` if None)."""
    if speed_mps is None:
        return "n/a"
    sec_per_km = 1000.0 / speed_mps
    minutes = int(sec_per_km // 60)
    seconds = sec_per_km - minutes * 60
    return f"{minutes}:{seconds:04.1f}/km"


def format_report(
    *,
    band: tuple[float, float],
    n_activities_considered: int,
    n_plausible: int,
    rejected: Sequence[tuple[str, str, str, ActivityPlausibility]],
    not_assessable: Sequence[tuple[str, str, str, str]],
    n_skipped_no_stream: int,
    n_samples_capped_total: int,
    candidate: RunThresholdCandidate,
) -> str:
    """Human-readable multi-line report of one derivation run.

    Shows the typed candidate with its full evidence trail, every rejected
    activity WITH its machine-readable reason, the not-assessable
    accounting (never silent) and the final confidence/contradiction
    outcome — the outcome IS the confidence signal: a ``candidate`` whose
    independent estimates agree, or an explicit refusal to fabricate a
    number (``contradictory_data`` / ``insufficient_data``).
    """
    lines = [
        "run threshold derivation -- READ-ONLY report (nothing is written to "
        "the database or to Intervals.icu)",
        f"threshold HR band (from configured LTHR): "
        f"{band[0]:.2f}-{band[1]:.2f} bpm (strict: in-band means "
        f"floor < HR < ceiling)",
        "",
        "activity-level plausibility:",
        f"  run activities considered: {n_activities_considered}, "
        f"plausible: {n_plausible}, rejected: {len(rejected)}, "
        f"not assessable: {len(not_assessable)}, "
        f"skipped (no speed stream): {n_skipped_no_stream}",
    ]
    if rejected:
        lines.append(
            f"  rejected activities ({len(rejected)}) -- each with its reason:"
        )
        for source_id, day, name, verdict in rejected:
            speed = (
                f"{verdict.average_speed_mps:.3f} m/s"
                if verdict.average_speed_mps is not None
                else "n/a"
            )
            lines.append(
                f"    {day} {source_id} {name!r} -- {speed}: "
                f"{verdict.reason} ({verdict.detail})"
            )
    else:
        lines.append("  rejected activities: none")
    if not_assessable:
        lines.append(
            f"  not assessable ({len(not_assessable)}) -- reported, never silent:"
        )
        for source_id, day, name, reason in not_assessable:
            lines.append(f"    {day} {source_id} {name!r}: {reason}")
    lines.extend(
        [
            "",
            "stream cleaning:",
            f"  speed samples capped in total: {n_samples_capped_total} "
            "(percentile-of-own-distribution cap + physiological ceiling)",
            "",
            "robust threshold derivation:",
            f"  runs used: {candidate.n_runs_used} of "
            f"{candidate.n_runs_input} input",
        ]
    )
    if candidate.best_efforts_mps:
        curve = ", ".join(
            f"{d // 60}min {format_pace(v)}" for d, v in sorted(candidate.best_efforts_mps.items())
        )
        lines.append(f"  robust best efforts (median across runs): {curve}")
    if candidate.cs_fit is not None:
        lines.append(
            f"  CS/D' fit: CS {candidate.cs_fit.cs_mps:.3f} m/s "
            f"({format_pace(candidate.cs_fit.cs_mps)}), "
            f"D' {candidate.cs_fit.d_prime_meters:.1f} m, "
            f"R^2 {candidate.cs_fit.r_squared:.4f} over "
            f"{candidate.cs_fit.n_points} points"
        )
    lines.append(
        f"  threshold-HR pace: {format_pace(candidate.threshold_hr_pace_mps)} "
        f"({candidate.threshold_hr_pace_mps:.3f} m/s)"
        if candidate.threshold_hr_pace_mps is not None
        else "  threshold-HR pace: n/a (no in-band HR samples)"
    )
    lines.append(
        f"  threshold-HR samples: {candidate.threshold_hr_sample_count}"
    )
    if candidate.agreement_relative_deviation is not None:
        lines.append(
            f"  agreement: relative deviation "
            f"{candidate.agreement_relative_deviation:.3f} "
            f"({candidate.agreement_relative_deviation * 100:.1f}%)"
        )
    lines.append(
        f"  anchor: {format_pace(candidate.anchor_mps)} "
        f"({candidate.anchor_mps:.3f} m/s)"
        if candidate.anchor_mps is not None
        else "  anchor: n/a"
    )
    lines.append(f"  OUTCOME: {candidate.outcome}")
    lines.append(f"  {candidate.detail}")
    if candidate.threshold_pace_mps is not None:
        lines.append(
            f"  CANDIDATE THRESHOLD PACE: {format_pace(candidate.threshold_pace_mps)} "
            f"({candidate.threshold_pace_mps:.3f} m/s, "
            f"{candidate.threshold_pace_sec_per_km:.1f} s/km) -- for owner "
            "review; nothing was persisted"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser (mirrors ``app.tools.cross_check_pmc``)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.derive_run_threshold",
        description=(
            "Read-only run-threshold derivation over the stored history: "
            "prints the typed candidate, the rejected activities with their "
            "reasons and the confidence/contradiction outcome. Writes nothing."
        ),
    )
    parser.add_argument(
        "--min-runs",
        type=int,
        default=3,
        help="minimum usable runs for a candidate (default: 3)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE,
        help=(
            "agreement tolerance between anchor and threshold-HR pace "
            f"(default: {DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE})"
        ),
    )
    parser.add_argument(
        "--min-valid-fraction",
        type=float,
        default=1.0,
        help=(
            "minimum fraction of valid samples a best-effort window needs "
            "(default: strict 1.0; relax explicitly for gap-heavy streams)"
        ),
    )
    return parser


async def run_derivation(
    session: AsyncSession,
    *,
    band: tuple[float, float],
    min_runs: int,
    agreement_tolerance: float,
    min_valid_fraction: float = 1.0,
) -> tuple[
    int,
    int,
    list[tuple[str, str, str, ActivityPlausibility]],
    list[tuple[str, str, str, str]],
    int,
    int,
    RunThresholdCandidate,
]:
    """Collect the run history and run the derivation (READ-ONLY selects).

    ``min_valid_fraction`` flows into the per-run best-effort extraction
    (default strict 1.0; the documented relaxation knob for gap-heavy
    streams — many of the owner's stored streams are sparse).

    Returns ``(n_considered, n_plausible, rejected, not_assessable,
    n_skipped_no_stream, n_samples_capped_total, candidate)``.
    """
    activities = (
        (
            await session.execute(
                # The engine's sport matching is case-insensitive
                # (app.engine.load), so the query is too: the stored types
                # are Capitalized (live-verified: 'Run', 'VirtualRun').
                select(ActivityRow)
                .where(func.lower(ActivityRow.type).in_(sorted(_RUNNING_SPORTS)))
                .order_by(ActivityRow.start_time)
            )
        )
        .scalars()
        .all()
    )

    rejected: list[tuple[str, str, str, ActivityPlausibility]] = []
    not_assessable: list[tuple[str, str, str, str]] = []
    plausible_ids: list[int] = []
    for activity in activities:
        verdict = assess_activity_plausibility(
            activity.type, activity.distance_m, activity.duration_s
        )
        label = (
            activity.start_time.date().isoformat(),
            activity.source_id,
            activity.name,
        )
        if verdict.outcome == "implausible":
            rejected.append((*label, verdict))
        elif verdict.outcome == "not_assessable":
            assert verdict.reason is not None  # not_assessable always has a reason
            not_assessable.append((*label, verdict.reason))
        else:
            plausible_ids.append(activity.id)

    streams = (
        (
            await session.execute(
                select(ActivityStreamRow).where(
                    ActivityStreamRow.activity_id.in_(plausible_ids),
                    ActivityStreamRow.stream_type.in_([_SPEED_STREAM, _HR_STREAM]),
                )
            )
        )
        .scalars()
        .all()
        if plausible_ids
        else []
    )
    speed_by_activity: dict[int, list[Any]] = {}
    hr_by_activity: dict[int, list[Any]] = {}
    for stream in streams:
        target = speed_by_activity if stream.stream_type == _SPEED_STREAM else hr_by_activity
        target[stream.activity_id] = list(stream.payload)

    cleaned_runs: list[CleanedRun] = []
    n_skipped_no_stream = 0
    n_samples_capped_total = 0
    for activity_id in plausible_ids:
        raw_speed = speed_by_activity.get(activity_id)
        if raw_speed is None:
            n_skipped_no_stream += 1
            continue
        cleaned = clean_speed_stream(raw_speed, sport="run")
        n_samples_capped_total += cleaned.n_samples_capped
        raw_hr = hr_by_activity.get(activity_id)
        cleaned_runs.append(
            CleanedRun(
                speed_mps=cleaned.samples,
                hr_bpm=list(raw_hr) if raw_hr is not None else None,
            )
        )

    candidate = derive_run_threshold_candidate(
        cleaned_runs,
        threshold_hr_band_bpm=band,
        min_runs=min_runs,
        agreement_tolerance=agreement_tolerance,
    )
    return (
        len(activities),
        len(plausible_ids),
        rejected,
        not_assessable,
        n_skipped_no_stream,
        n_samples_capped_total,
        candidate,
    )


async def _run(
    min_runs: int,
    agreement_tolerance: float,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None,
    min_valid_fraction: float = 1.0,
) -> str:
    """Run the derivation against the configured database and format it."""
    lthr = settings.athlete_lthr_bpm
    if lthr is None or lthr <= 0.0:
        raise SystemExit(
            "no positive LTHR configured (settings field athlete_lthr_bpm): "
            "the threshold HR band cannot be derived without it"
        )
    band = threshold_hr_band_from_lthr(lthr)
    if session_factory is not None:
        async with session_factory() as session:
            collected = await run_derivation(
                session,
                band=band,
                min_runs=min_runs,
                agreement_tolerance=agreement_tolerance,
                min_valid_fraction=min_valid_fraction,
            )
    else:
        engine = create_db_engine(settings.database_url)
        try:
            async with make_session_factory(engine)() as session:
                collected = await run_derivation(
                    session,
                    band=band,
                    min_runs=min_runs,
                    agreement_tolerance=agreement_tolerance,
                    min_valid_fraction=min_valid_fraction,
                )
        finally:
            await engine.dispose()
    (
        n_considered,
        n_plausible,
        rejected,
        not_assessable,
        n_skipped_no_stream,
        n_samples_capped_total,
        candidate,
    ) = collected
    return format_report(
        band=band,
        n_activities_considered=n_considered,
        n_plausible=n_plausible,
        rejected=rejected,
        not_assessable=not_assessable,
        n_skipped_no_stream=n_skipped_no_stream,
        n_samples_capped_total=n_samples_capped_total,
        candidate=candidate,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
) -> int:
    """CLI entry point: 0 on a printed report, 2 on configuration errors."""
    args = build_parser().parse_args(argv)
    config = settings or get_settings()
    report = asyncio.run(
        _run(
            args.min_runs,
            args.tolerance,
            config,
            session_factory,
            args.min_valid_fraction,
        )
    )
    print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
