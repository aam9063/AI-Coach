"""CLI: recompute and persist the readiness / intensity / durability engine
outputs for a trailing window (RID-10, §6).

Usage::

    python -m app.db.engine_outputs --days N [--athlete-id ID] [--end YYYY-MM-DD]

Mirrors ``python -m app.db.daily_load`` in style: it recomputes the three
engine output families from the stored data via the services in
:mod:`app.services.readiness` / :mod:`app.services.intensity` /
:mod:`app.services.durability`, upserts the ``readiness_snapshot``,
``weekly_intensity`` and ``session_durability`` rows (one commit at the
end) and prints a report with per-family counts and the FULL skipped /
no-data accounting — nothing is ever dropped silently.

Inputs and where they come from:

- readiness: ``wellness`` (HRV ln(rMSSD), resting HR, sleep) plus the TSB
  context from the ``daily_load`` ``combined`` rows — run
  ``python -m app.db.daily_load`` first so the window's TSB exists;
  window days without it are reported as skipped.
- intensity: the window's activities and streams; the per-sport source
  table is the engine's canonical DEFAULT map (run: Friel HR zones, bike:
  Coggan power zones, swim: CSS zones — the settings'
  ``engine_sport_modality`` is pinned to it and validated), refined per
  session by stream availability (the load engine's power-first
  preference): a POWER-LESS ride with HR classifies on the ``bike_hr``
  Friel HR table, so the owner's rides produce weekly intensity rows
  under the bike sport. A session with no usable modality is reported as
  skipped (never classified with a guessed threshold), as is a session
  whose selected modality's threshold (FTP / LTHR / CSS) is not
  configured.
- durability: bike (NP) / run (NGS) activities with the intensity and HR
  streams.

All engine constants are settings-sourced through
:func:`app.db.engine_readiness_config` (RID-10a's mapping; its "no caller
yet" note is now historical: this CLI is a caller) and passed into the
services — never re-hardcoded here.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from typing import Protocol

from app.core.settings import get_settings
from app.db.engine_readiness_config import (
    durability_constants_from_settings,
    intensity_constants_from_settings,
    readiness_constants_from_settings,
)
from app.db.session import create_db_engine, make_session_factory
from app.services.durability import DurabilityReport, recompute_durability
from app.services.intensity import IntensityReport, recompute_intensity
from app.services.readiness import ReadinessReport, recompute_readiness

__all__ = [
    "build_parser",
    "format_report",
    "main",
]


class _Skips(Protocol):
    """Structural type of the services' skip entries (activity_id/sport or
    date, plus the reason) — only ``reason`` is printed."""

    @property
    def reason(self) -> str: ...


def _format_skips(skips: Sequence[_Skips]) -> list[str]:
    """One line per skipped item with its concrete reason."""
    return [f"  {skip.reason}" for skip in skips]


def format_report(
    *,
    window_start: dt.date,
    window_end: dt.date,
    engine_version: str,
    readiness: ReadinessReport,
    intensity: IntensityReport,
    durability: DurabilityReport,
) -> str:
    """Human-readable multi-line report of one recompute run."""
    lines = [
        f"engine outputs recompute: window {window_start}..{window_end} "
        f"(engine_version {engine_version})",
        "",
        "readiness (readiness_snapshot, one row per athlete+date):",
        f"  days considered: {readiness.days_considered}, "
        f"rows upserted: {readiness.rows_upserted}",
    ]
    if readiness.skipped:
        lines.append(
            f"  skipped days ({len(readiness.skipped)}) — never silent:"
        )
        lines.extend(_format_skips(readiness.skipped))
    else:
        lines.append("  skipped days: none")

    lines.extend(
        [
            "",
            "intensity (weekly_intensity, one row per athlete+ISO week+sport):",
            f"  activities considered: {intensity.activities_considered}, "
            f"sessions derived: {intensity.sessions_derived}, "
            f"weeks persisted: {intensity.weeks_persisted}, "
            f"rows upserted: {intensity.rows_upserted} "
            f"(of which no_data: {intensity.no_data_rows})",
        ]
    )
    if intensity.skipped:
        lines.append(
            f"  skipped activities ({len(intensity.skipped)}) — never silent:"
        )
        lines.extend(_format_skips(intensity.skipped))
    else:
        lines.append("  skipped activities: none")

    lines.extend(
        [
            "",
            "durability (session_durability, one row per activity):",
            f"  activities considered: {durability.activities_considered}, "
            f"rows upserted: {durability.rows_upserted} "
            f"(of which not_steady: {durability.not_steady_rows})",
        ]
    )
    if durability.skipped:
        lines.append(
            f"  skipped activities ({len(durability.skipped)}) — never silent:"
        )
        lines.extend(_format_skips(durability.skipped))
    else:
        lines.append("  skipped activities: none")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the engine-outputs CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.db.engine_outputs",
        description=(
            "Recompute the deterministic engine's readiness, weekly "
            "intensity and session durability outputs from the stored "
            "data and upsert their rows for a trailing window of days. "
            "Prints per-family counts plus the full skipped/no-data "
            "accounting."
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

    readiness_constants = readiness_constants_from_settings(settings)
    intensity_constants = intensity_constants_from_settings(settings)
    durability_constants = durability_constants_from_settings(settings)

    async def _run() -> tuple[ReadinessReport, IntensityReport, DurabilityReport]:
        engine = create_db_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                readiness = await recompute_readiness(
                    session,
                    athlete_id=args.athlete_id,
                    window_end=window_end,
                    days=args.days,
                    engine_version=settings.engine_version,
                    **readiness_constants,
                )
                intensity = await recompute_intensity(
                    session,
                    athlete_id=args.athlete_id,
                    window_end=window_end,
                    days=args.days,
                    engine_version=settings.engine_version,
                    sport_modality=intensity_constants["sport_modality"],
                    first_threshold_pcts=intensity_constants[
                        "first_threshold_pcts"
                    ],
                    second_threshold_pcts=intensity_constants[
                        "second_threshold_pcts"
                    ],
                    ftp_watts=settings.athlete_ftp_w,
                    lthr_bpm=settings.athlete_lthr_bpm,
                    css_mps=settings.athlete_css_speed_mps,
                )
                durability = await recompute_durability(
                    session,
                    athlete_id=args.athlete_id,
                    window_end=window_end,
                    days=args.days,
                    engine_version=settings.engine_version,
                    reference_band=durability_constants["reference_band"],
                    max_intensity_drift=durability_constants[
                        "max_intensity_drift"
                    ],
                    np_window_samples=settings.engine_np_window_samples,
                    np_min_valid_fraction=settings.engine_min_valid_fraction,
                )
                await session.commit()
                return readiness, intensity, durability
        finally:
            await engine.dispose()

    readiness, intensity, durability = asyncio.run(_run())
    print(
        format_report(
            window_start=window_end - dt.timedelta(days=args.days - 1),
            window_end=window_end,
            engine_version=settings.engine_version,
            readiness=readiness,
            intensity=intensity,
            durability=durability,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
