"""Athlete-threshold extraction and CLI (ODD LOAD-11, first half).

Extracts the owner's athlete thresholds from the Intervals.icu
sport-settings and athlete-profile endpoints into a typed
:class:`AthleteThresholds` record for the deterministic load engine (§7.1):

- **Cycling FTP** (watts): ``ftp`` on the sport-settings entry whose
  ``types`` include ``"Ride"``.
- **Run threshold speed** (m/s): ``threshold_pace`` on the ``"Run"`` entry.
- **Swim CSS speed** (m/s): ``threshold_pace`` on the ``"Swim"`` entry
  (live-verified 0.8333333 m/s on 2026-10-02).
- **LTHR and max HR per sport** (bpm): ``lthr``/``max_hr`` on each sport
  entry.
- **Resting HR and weight**: ``icu_resting_hr``/``weight`` on the athlete
  profile.

Ownership and authority (§5.1): Intervals.icu is the *editing surface* for
these values — owner-editable configuration, never an authoritative computed
metric. Missing values stay ``None`` and are listed in
:attr:`AthleteThresholds.gaps`; nothing is ever silently defaulted.

Endpoint payload shapes were verified against the live API on 2026-10-02
(see :mod:`app.ingest.models` and :mod:`app.ingest.client` docstrings).

Entry points:

- :func:`extract_athlete_thresholds` — pure mapping function (no I/O).
- :func:`main` — argparse CLI (``python -m app.ingest.thresholds
  [--athlete-id ID]``) with an injectable client for tests. It prints (a)
  a threshold summary, (b) ready-to-paste ``.env`` lines, and (c) a GAPS
  list on stderr. It exits 0 even with gaps and **never writes any file**.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.core.settings import Settings, get_settings
from app.ingest.client import IntervalsClient
from app.ingest.exceptions import IntervalsError
from app.ingest.models import AthleteProfile, SportSettings

# Gap keys, in the fixed order they are reported. Keys match the
# AthleteThresholds field names so tests and tooling can assert on them.
_GAP_ORDER: tuple[str, ...] = (
    "ftp_w",
    "run_threshold_speed_mps",
    "css_speed_mps",
    "lthr_bike_bpm",
    "lthr_run_bpm",
    "lthr_swim_bpm",
    "hr_max_bike_bpm",
    "hr_max_run_bpm",
    "hr_max_swim_bpm",
    "hr_rest_bpm",
    "weight_kg",
)

_GAP_DESCRIPTIONS: dict[str, str] = {
    "ftp_w": 'cycling FTP (watts; "ftp" on the "Ride" sport-settings entry)',
    "run_threshold_speed_mps": (
        'run threshold speed (m/s; "threshold_pace" on the "Run" entry)'
    ),
    "css_speed_mps": (
        'swim CSS speed (m/s; "threshold_pace" on the "Swim" entry)'
    ),
    "lthr_bike_bpm": 'cycling LTHR (bpm; "lthr" on the "Ride" entry)',
    "lthr_run_bpm": 'run LTHR (bpm; "lthr" on the "Run" entry)',
    "lthr_swim_bpm": 'swim LTHR (bpm; "lthr" on the "Swim" entry)',
    "hr_max_bike_bpm": 'cycling max HR (bpm; "max_hr" on the "Ride" entry)',
    "hr_max_run_bpm": 'run max HR (bpm; "max_hr" on the "Run" entry)',
    "hr_max_swim_bpm": 'swim max HR (bpm; "max_hr" on the "Swim" entry)',
    "hr_rest_bpm": (
        'resting HR (bpm; "icu_resting_hr" on the athlete profile)'
    ),
    "weight_kg": 'body weight (kg; "weight" on the athlete profile)',
}


@dataclass
class AthleteThresholds:
    """Owner-editable athlete thresholds extracted per sport (§5.1, §7.1).

    Every field is ``None`` when the value is missing on Intervals.icu; each
    missing value is also named in :attr:`gaps` so callers report it
    explicitly instead of silently defaulting. These are configuration
    values sourced from Intervals.icu as the editing surface, never
    authoritative computed metrics (§5.1).
    """

    ftp_w: float | None = None
    run_threshold_speed_mps: float | None = None
    css_speed_mps: float | None = None
    lthr_bike_bpm: float | None = None
    lthr_run_bpm: float | None = None
    lthr_swim_bpm: float | None = None
    hr_max_bike_bpm: float | None = None
    hr_max_run_bpm: float | None = None
    hr_max_swim_bpm: float | None = None
    hr_rest_bpm: float | None = None
    weight_kg: float | None = None
    gaps: list[str] = field(default_factory=list)


class ThresholdsClientProtocol(Protocol):
    """Duck-typed subset of ``IntervalsClient`` needed by this module."""

    def get_sport_settings(self) -> list[SportSettings]: ...

    def get_athlete_profile(self) -> AthleteProfile: ...


def _entry_for(sport_settings: list[SportSettings], sport: str) -> SportSettings | None:
    """First sport-settings entry whose ``types`` include ``sport``."""
    for entry in sport_settings:
        if sport in entry.types:
            return entry
    return None


def extract_athlete_thresholds(
    sport_settings: list[SportSettings], athlete: AthleteProfile
) -> AthleteThresholds:
    """Map Intervals.icu sport settings + athlete profile to thresholds.

    Pure function (no I/O). Per sport:

    - cycling (``"Ride"`` entry): ``ftp`` (watts), ``lthr``, ``max_hr``;
    - running (``"Run"`` entry): ``threshold_pace`` (m/s), ``lthr``,
      ``max_hr``;
    - swimming (``"Swim"`` entry): ``threshold_pace`` (m/s = CSS speed),
      ``lthr``, ``max_hr``.

    Athlete-level: ``icu_resting_hr`` and ``weight`` from the profile.
    Missing values remain ``None`` and are collected — in the fixed
    :data:`_GAP_ORDER` — into ``AthleteThresholds.gaps`` (§5.1: explicit
    gaps, never silent defaults).
    """
    ride = _entry_for(sport_settings, "Ride")
    run = _entry_for(sport_settings, "Run")
    swim = _entry_for(sport_settings, "Swim")

    values: dict[str, float | None] = {
        "ftp_w": ride.ftp if ride else None,
        "run_threshold_speed_mps": run.threshold_pace if run else None,
        "css_speed_mps": swim.threshold_pace if swim else None,
        "lthr_bike_bpm": ride.lthr if ride else None,
        "lthr_run_bpm": run.lthr if run else None,
        "lthr_swim_bpm": swim.lthr if swim else None,
        "hr_max_bike_bpm": ride.max_hr if ride else None,
        "hr_max_run_bpm": run.max_hr if run else None,
        "hr_max_swim_bpm": swim.max_hr if swim else None,
        "hr_rest_bpm": athlete.icu_resting_hr,
        "weight_kg": athlete.weight,
    }
    thresholds = AthleteThresholds(
        ftp_w=values["ftp_w"],
        run_threshold_speed_mps=values["run_threshold_speed_mps"],
        css_speed_mps=values["css_speed_mps"],
        lthr_bike_bpm=values["lthr_bike_bpm"],
        lthr_run_bpm=values["lthr_run_bpm"],
        lthr_swim_bpm=values["lthr_swim_bpm"],
        hr_max_bike_bpm=values["hr_max_bike_bpm"],
        hr_max_run_bpm=values["hr_max_run_bpm"],
        hr_max_swim_bpm=values["hr_max_swim_bpm"],
        hr_rest_bpm=values["hr_rest_bpm"],
        weight_kg=values["weight_kg"],
    )
    thresholds.gaps = [key for key in _GAP_ORDER if values[key] is None]
    return thresholds


def _fmt(value: float | None, unit: str) -> str:
    """Format one threshold for the summary; missing values stay explicit."""
    if value is None:
        return "missing"
    if float(value).is_integer():
        return f"{int(value)} {unit}"
    return f"{value:.4f}".rstrip("0").rstrip(".") + f" {unit}"


def _summary_lines(thresholds: AthleteThresholds) -> list[str]:
    """Human-readable threshold summary (stdout)."""
    return [
        "Athlete thresholds (owner configuration; Intervals.icu is the editing",
        "surface, NOT an authoritative metric — §5.1)",
        f"  FTP (cycling):            {_fmt(thresholds.ftp_w, 'W')}",
        f"  Run threshold speed:      {_fmt(thresholds.run_threshold_speed_mps, 'm/s')}",
        f"  Swim CSS speed:           {_fmt(thresholds.css_speed_mps, 'm/s')}",
        f"  LTHR bike/run/swim:       {_fmt(thresholds.lthr_bike_bpm, 'bpm')}"
        f" / {_fmt(thresholds.lthr_run_bpm, 'bpm')} / {_fmt(thresholds.lthr_swim_bpm, 'bpm')}",
        f"  Max HR bike/run/swim:     {_fmt(thresholds.hr_max_bike_bpm, 'bpm')}"
        f" / {_fmt(thresholds.hr_max_run_bpm, 'bpm')} / {_fmt(thresholds.hr_max_swim_bpm, 'bpm')}",
        f"  Resting HR:               {_fmt(thresholds.hr_rest_bpm, 'bpm')}",
        f"  Weight:                   {_fmt(thresholds.weight_kg, 'kg')}",
    ]


def _env_line(name: str, value: float | None) -> str:
    """One ready-to-paste .env line; missing values get an empty value."""
    if value is None:
        return f"{name}="
    if float(value).is_integer():
        return f"{name}={int(value)}"
    return f"{name}={value}"


def _env_lines(thresholds: AthleteThresholds) -> list[str]:
    """Ready-to-paste .env lines (stdout)."""
    return [
        "# Ready-to-paste .env lines (owner configuration; do not commit real",
        "# values — keep them in .env / the environment only, §14).",
        _env_line("ATHLETE_FTP_W", thresholds.ftp_w),
        _env_line("ATHLETE_LTHR_BPM", thresholds.lthr_bike_bpm),
        _env_line("ATHLETE_HR_MAX_BPM", thresholds.hr_max_bike_bpm),
        _env_line("ATHLETE_HR_REST_BPM", thresholds.hr_rest_bpm),
        _env_line("ATHLETE_CSS_SPEED_MPS", thresholds.css_speed_mps),
        _env_line("ATHLETE_THRESHOLD_RUN_SPEED_MPS", thresholds.run_threshold_speed_mps),
        _env_line("ATHLETE_WEIGHT_KG", thresholds.weight_kg),
    ]


def _gap_lines(thresholds: AthleteThresholds) -> list[str]:
    """Explicit GAPS report (stderr); missing values are never defaulted."""
    if not thresholds.gaps:
        return ["GAPS: none — all athlete thresholds present."]
    lines = [
        "GAPS (missing athlete thresholds — left as None, never silently",
        "defaulted; set them manually in .env or fill them in on Intervals.icu):",
    ]
    lines.extend(f"  - {key}: {_GAP_DESCRIPTIONS[key]}" for key in thresholds.gaps)
    return lines


def build_parser() -> argparse.ArgumentParser:
    """Build the thresholds CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest.thresholds",
        description=(
            "Fetch athlete thresholds from Intervals.icu (sport settings + "
            "athlete profile), print a summary and ready-to-paste .env lines, "
            "and report missing values as explicit GAPS on stderr. "
            "Prints only; never writes any file."
        ),
    )
    parser.add_argument(
        "--athlete-id",
        default=None,
        metavar="ID",
        help=(
            "Intervals.icu athlete id to query (default: the configured "
            "INTERVALS_ATHLETE_ID; 0 = the API key's owner)"
        ),
    )
    return parser


def fetch_thresholds(client: ThresholdsClientProtocol) -> AthleteThresholds:
    """Fetch both endpoints and run the pure extraction (test seam)."""
    sport_settings = client.get_sport_settings()
    athlete = client.get_athlete_profile()
    return extract_athlete_thresholds(sport_settings, athlete)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: ThresholdsClientProtocol | None = None,
    settings: Settings | None = None,
) -> int:
    """CLI entry point; returns 0 even when gaps are reported.

    With no injected client the real :class:`IntervalsClient` is built from
    settings. On an API failure prints the error to stderr and returns 1.
    Never writes any file.
    """
    args = build_parser().parse_args(argv)
    config = settings or get_settings()
    if args.athlete_id:
        config = config.model_copy(update={"intervals_athlete_id": args.athlete_id})
    try:
        if client is None:
            with IntervalsClient(config) as real_client:
                thresholds = fetch_thresholds(real_client)
        else:
            thresholds = fetch_thresholds(client)
    except IntervalsError as exc:
        print(f"FAILED to fetch athlete thresholds: {exc}", file=sys.stderr)
        return 1

    for line in _summary_lines(thresholds):
        print(line)
    print()
    for line in _env_lines(thresholds):
        print(line)
    print(file=sys.stderr)
    for line in _gap_lines(thresholds):
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
