"""Weekly-intensity persistence service (RID-10) — the §6 integration layer.

Thin, typed bridge between the stored activity streams and the pure engine
(``app.engine.intensity``). Lives OUTSIDE ``app/engine`` because it does
I/O (§6 purity rule).

Pipeline of :func:`recompute_intensity` over a trailing window of ``days``
calendar days ending at ``window_end``:

1. Read the window's activities with their streams (shared loading logic
   with the daily-load service: the local session date via
   :func:`app.services.daily_load._activity_date` — deliberate reuse, the
   week attribution must match the load model's day attribution).
2. Map each activity type onto one of the engine's three intensity sports
   (``run``/``bike``/``swim``) with the same Strava-style type families
   the load engine documents (Ride/VirtualRide/... -> bike; Run/TrailRun/
   TreadmillRun/VirtualRun -> run; Swim -> swim). Anything else (strength,
   walks) is reported in :attr:`IntensityReport.skipped` — never silently
   dropped.
3. Select the session's source-table modality with
   :func:`app.engine.intensity.select_intensity_modality` — the
   documented DEFAULT map (:data:`SPORT_MODALITY`) refined by stream
   availability, mirroring the load engine's preference order (power
   first, then heart rate): a bike ride with usable power samples
   classifies on the Coggan power table; a POWER-LESS ride (the owner
   has no power meter) with usable HR samples on the ``bike_hr`` Friel
   HR table (LT1 90% / LT2 100% LTHR). Usability is the load engine's
   rule (:func:`app.engine.intensity.has_usable_samples`): a stream that
   is absent, empty or ENTIRELY ``None`` is not usable. A session with
   no usable modality is reported in :attr:`IntensityReport.skipped`
   with the named reason — never silently dropped, never guessed. The
   caller's ``sport_modality`` map is still validated to EQUAL the
   canonical DEFAULT map and rejected loudly otherwise (the settings
   field ``engine_sport_modality`` stays owner-reviewable but is pinned
   to the engine's mapping; selection refines per SESSION, not per
   settings). The selected modality's threshold (FTP / LTHR / CSS) must
   be configured or the activity is skipped with a reason — never
   guessed. The classification validates against the table the session
   was ACTUALLY classified on (``ZoneSession.modality``), so ``bike_hr``
   zone keys flow through the weekly aggregation unharmed.
4. Weight the classified samples into source-zone SECONDS, excluding
   non-moving/paused time (the pause rule of
   :func:`app.engine.intensity.moving_weights` — see the real-data
   finding in the engine's module docstring): with a ``time`` stream the
   interval ``[t_i, t_i+1)`` is attributed to sample ``i``'s zone (the
   last valid sample inherits the previous gap; intervals whose sample is
   a ``None`` gap contribute nothing — the engine's gap rule), a sample
   whose speed is tolerantly at or below the stopped-speed tolerance
   (settings ``ENGINE_PAUSE_SPEED_TOLERANCE_MPS``, the FIT SDK's 0.1 m/s
   default) contributes NOTHING, and an interval at or beyond the gap cap
   (settings ``ENGINE_PAUSE_GAP_CAP_MEDIAN_MULTIPLE``, 5x the median
   positive sample interval — the general FALLBACK when no speed stream
   exists, where a pause only shows up as a large timestamp gap)
   contributes nothing. Without a ``time`` stream each valid sample counts
   as exactly one second (the §6 per-second convention); the speed rule
   still applies, the gap cap cannot. Boundary convention (the project's
   strict rule): EXACTLY at a limit counts as beyond it. The seconds are
   mapped into the 3-zone model with
   :func:`app.engine.intensity.map_to_three_zones` and the
   caller-supplied per-modality cut points (settings
   ``ENGINE_*_THRESHOLD_PCTS``).
5. Aggregate with :func:`app.engine.intensity.weekly_time_in_zone` (ISO
   weeks, Monday start) and upsert one ``weekly_intensity`` row per
   (athlete, ISO week, sport) via
   :func:`app.db.repository.upsert_weekly_intensity`, stamped with
   ``engine_version`` (§6) and ``computed_at``.

``no_data`` contract (ODD decision): every sport-week the engine reports
is persisted, INCLUDING the weeks where a sport has no session — as a row
with ``status = "no_data"``, zero seconds and ``percentages IS NULL``. A
week without data is stored AS missing data, never as a fabricated 0%
split (§7.5). The engine's descriptive pattern comparison (RID-7) is NOT
persisted: it is a pure function of the persisted percentages plus the
settings' reference bands, so a stored copy would be redundant.

All writes flush without committing; the caller owns the transaction.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import upsert_weekly_intensity
from app.engine.intensity import (
    DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE,
    DEFAULT_PAUSE_SPEED_TOLERANCE_MPS,
    SPORT_KEYS,
    SPORT_MODALITY,
    IntensityModalityKey,
    SportKey,
    WeeklyIntensity,
    has_usable_samples,
    moving_weights,
    select_intensity_modality,
    three_zone_model,
    weekly_time_in_zone,
    zone_session,
)
from app.engine.zones import hr_zone_for, power_zone_for, swim_zone_for
from app.services.daily_load import (
    _activity_date,
    _load_window_activities,
)

__all__ = [
    "IntensityReport",
    "SkippedActivity",
    "recompute_intensity",
]


_CYCLING_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset(
    {"ride", "virtualride", "gravelride", "mountainbikeride", "ebikeride"}
)
"""Activity types mapping to the ``bike`` intensity sport (the same
Strava-style type families the load engine documents; app.engine.load)."""

_RUNNING_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset(
    {"run", "trailrun", "treadmillrun", "virtualrun"}
)
"""Activity types mapping to the ``run`` intensity sport."""

_SWIMMING_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset({"swim"})
"""Activity types mapping to the ``swim`` intensity sport."""


@dataclass(frozen=True)
class SkippedActivity:
    """One activity that contributed no intensity data, with the reason."""

    activity_id: int
    sport: str
    reason: str


@dataclass(frozen=True)
class IntensityReport:
    """Outcome of one :func:`recompute_intensity` run (human + test facing).

    ``sessions_derived`` counts activities that became engine
    :class:`~app.engine.intensity.ZoneSession` inputs; ``weeks_persisted``
    the ISO weeks aggregated; ``rows_upserted`` the persisted rows (weeks
    x sports, including ``no_data`` rows); ``no_data_rows`` counts the
    persisted ``no_data`` sport-weeks; ``skipped`` lists every
    considered-but-unclassifiable activity with its reason.
    """

    window_start: dt.date
    window_end: dt.date
    engine_version: str
    activities_considered: int
    sessions_derived: int
    weeks_persisted: int
    rows_upserted: int
    no_data_rows: int
    skipped: tuple[SkippedActivity, ...] = field(default_factory=tuple)


def _sport_key(activity_type: str) -> SportKey | None:
    """The intensity sport of a stored activity type, or ``None`` when the
    type is not one of the three intensity sports."""
    normalized = activity_type.strip().lower()
    if normalized in _CYCLING_ACTIVITY_TYPES:
        return "bike"
    if normalized in _RUNNING_ACTIVITY_TYPES:
        return "run"
    if normalized in _SWIMMING_ACTIVITY_TYPES:
        return "swim"
    return None


def _required_stream(modality: IntensityModalityKey) -> str:
    """The stored stream the modality classifies (canonical ingest names)."""
    if modality == "bike_power":
        return "power"
    if modality in ("run_hr", "bike_hr"):
        return "hr"
    return "speed"


def _required_threshold(modality: IntensityModalityKey) -> str:
    """Human label of the threshold the modality's classification needs."""
    if modality == "bike_power":
        return "FTP"
    if modality in ("run_hr", "bike_hr"):
        return "LTHR"
    return "CSS"


def _classify_samples(
    modality: IntensityModalityKey,
    values: list[float | None],
    *,
    ftp_watts: float | None,
    lthr_bpm: float | None,
    css_mps: float | None,
) -> list[str | None]:
    """Classify each sample into its source-zone key (``None`` = gap).

    Mirrors the Feature 4 tables exactly (the same classifier functions
    the zone tables are built from). A swim speed sample is converted to
    the pace-per-100 m axis the swim zones are defined on; a non-positive
    speed (stopped athlete) is a gap, never a zone.
    """
    classified: list[str | None] = []
    for value in values:
        if value is None:
            classified.append(None)
            continue
        if modality == "bike_power":
            assert ftp_watts is not None  # caller validated
            classified.append(power_zone_for(value, ftp_watts).key)
        elif modality in ("run_hr", "bike_hr"):
            assert lthr_bpm is not None  # caller validated
            zone_sport: Literal["run", "bike"] = (
                "run" if modality == "run_hr" else "bike"
            )
            classified.append(hr_zone_for(zone_sport, value, lthr_bpm).key)
        else:
            assert css_mps is not None  # caller validated
            if value <= 0.0:
                classified.append(None)
                continue
            pace_sec_per_100m = 100.0 / value
            classified.append(swim_zone_for(pace_sec_per_100m, css_mps).key)
    return classified


def _weighted_zone_seconds(
    classified: list[str | None],
    time_stream: list[Any] | None,
    speed_stream: list[Any] | None,
    *,
    speed_tolerance_mps: float,
    gap_cap_median_multiple: float,
) -> dict[str, float]:
    """Source-zone seconds from the classified samples (module-docstring
    weighting rule, step 4): the per-sample weights come from the pure
    engine rule :func:`app.engine.intensity.moving_weights` — with a
    ``time`` stream the interval to the next sample is attributed to the
    sample's zone (last sample inherits the previous gap; a ``None``-gap
    sample contributes nothing), NON-MOVING samples (speed tolerantly at
    or below ``speed_tolerance_mps``) and pause/recording gaps (interval
    at or beyond ``gap_cap_median_multiple`` x the median positive sample
    interval — the fallback when no speed stream exists) contribute
    nothing; without a ``time`` stream each valid sample counts as one
    second (per-second convention, §6)."""
    n = len(classified)
    if n == 0:
        return {}
    times: list[float] | None = None
    if time_stream is not None and len(time_stream) >= n:
        times = [float(t) for t in time_stream[:n]]
    speeds: list[float | None] | None = None
    if speed_stream is not None and len(speed_stream) >= n:
        speeds = [
            None if s is None else float(s) for s in speed_stream[:n]
        ]
    weights = moving_weights(
        n,
        times=times,
        speeds=speeds,
        speed_tolerance_mps=speed_tolerance_mps,
        gap_cap_median_multiple=gap_cap_median_multiple,
    )
    seconds: dict[str, float] = defaultdict(float)
    for i, zone_key in enumerate(classified):
        if zone_key is None:
            continue  # the engine's gap rule: a gap contributes no time
        if weights[i] > 0.0:
            seconds[zone_key] += weights[i]
    return dict(seconds)


def _build_zone_session(
    activity: Any,
    streams: dict[str, list[Any]],
    *,
    sport: SportKey,
    modality: IntensityModalityKey,
    ftp_watts: float | None,
    lthr_bpm: float | None,
    css_mps: float | None,
    speed_tolerance_mps: float = DEFAULT_PAUSE_SPEED_TOLERANCE_MPS,
    gap_cap_median_multiple: float = DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE,
) -> tuple[Any | None, str | None]:
    """Derive one engine :class:`ZoneSession` from a stored activity.

    ``modality`` is the SELECTED source table (the refined choice of
    :func:`app.engine.intensity.select_intensity_modality` — e.g.
    ``bike_hr`` for a power-less ride). Returns ``(session, None)`` on
    success or ``(None, reason)`` when the modality's stream or threshold
    is missing — reported, never guessed. A session whose samples are ALL
    gaps is a valid empty ZoneSession (the engine's documented "session
    without intensity data" semantics).
    """
    threshold = {
        "bike_power": ftp_watts,
        "run_hr": lthr_bpm,
        "bike_hr": lthr_bpm,
        "swim_pace": css_mps,
    }[modality]
    if threshold is None:
        return None, (
            f"{_required_threshold(modality)} not configured; the "
            f"{modality} zone classification cannot run — never guessed"
        )
    stream_name = _required_stream(modality)
    if stream_name not in streams:
        return None, (
            f"no {stream_name!r} stream: the {modality} modality cannot "
            "classify this activity"
        )
    values = list(streams[stream_name])
    time_stream = streams.get("time")
    speed_stream = streams.get("speed")
    classified = _classify_samples(
        modality,
        values,
        ftp_watts=ftp_watts,
        lthr_bpm=lthr_bpm,
        css_mps=css_mps,
    )
    zone_seconds = _weighted_zone_seconds(
        classified,
        time_stream,
        speed_stream,
        speed_tolerance_mps=speed_tolerance_mps,
        gap_cap_median_multiple=gap_cap_median_multiple,
    )
    return (
        zone_session(
            _activity_date(activity), sport, zone_seconds, modality=modality
        ),
        None,
    )


async def recompute_intensity(
    session: AsyncSession,
    *,
    window_end: dt.date,
    days: int,
    engine_version: str,
    sport_modality: dict[SportKey, IntensityModalityKey],
    first_threshold_pcts: dict[IntensityModalityKey, float],
    second_threshold_pcts: dict[IntensityModalityKey, float],
    athlete_id: int = 1,
    ftp_watts: float | None = None,
    lthr_bpm: float | None = None,
    css_mps: float | None = None,
    speed_tolerance_mps: float = DEFAULT_PAUSE_SPEED_TOLERANCE_MPS,
    gap_cap_median_multiple: float = DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE,
) -> IntensityReport:
    """Recompute and persist the weekly 3-zone intensity rows for a
    trailing window.

    See the module docstring for the pipeline and the no-data contract.
    ``days`` must be positive; the window is the ``days`` calendar days
    ending at (and including) ``window_end``. The modality map and the
    per-modality cut points come from the caller (the CLI passes the
    settings' ``engine_sport_modality`` and ``ENGINE_*_THRESHOLD_PCTS``
    maps via :func:`app.db.engine_readiness_config.
    intensity_constants_from_settings`); ``sport_modality`` must equal the
    engine's canonical ``SPORT_MODALITY`` (validated, ``ValueError``
    otherwise). The athlete thresholds come from settings (§14) and stay
    ``None`` when unconfigured — the affected activities are then reported
    as skipped, never classified with a guessed threshold. The pause rule
    is configurable: ``speed_tolerance_mps`` (the stopped-speed tolerance,
    default :data:`DEFAULT_PAUSE_SPEED_TOLERANCE_MPS`) and
    ``gap_cap_median_multiple`` (the pause/recording-gap cap as a multiple
    of the median positive sample interval — the general fallback when no
    speed stream exists, default
    :data:`DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE`).

    Upserts are idempotent per ``(athlete_id, iso_year, iso_week,
    sport)``; flushes without committing.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days!r}")
    if sport_modality != SPORT_MODALITY or set(sport_modality) != set(SPORT_KEYS):
        # The settings' sport->modality map is pinned to the engine's
        # canonical DEFAULT mapping (SPORT_MODALITY): modality selection
        # refines the bike entry per SESSION by stream availability, never
        # per settings, so a different map cannot flow through — rejected
        # here loudly (the settings field engine_sport_modality stays
        # owner-reviewable but pinned).
        raise ValueError(
            f"sport_modality {sport_modality!r} does not match the "
            f"engine's canonical SPORT_MODALITY {SPORT_MODALITY!r}: the "
            "sport->modality map is pinned to the engine's default "
            "mapping; per-session stream availability is refined by "
            "select_intensity_modality, not by settings"
        )
    window_start = window_end - dt.timedelta(days=days - 1)

    candidates = await _load_window_activities(
        session, window_start=window_start, window_end=window_end
    )

    # Built up-front so a custom settings cut point that would split a
    # source zone fails LOUDLY here (three_zone_model raises), before any
    # row is written. bike_hr is validated alongside the canonical
    # default tables: modality selection can route a power-less bike
    # session onto it, so a bad settings cut point for it must fail the
    # same way.
    validated_modalities: Final[frozenset[IntensityModalityKey]] = (
        frozenset(sport_modality.values()) | {"bike_hr"}
    )
    for modality in sorted(validated_modalities):
        three_zone_model(
            modality,
            first_threshold_pct=first_threshold_pcts[modality],
            second_threshold_pct=second_threshold_pcts[modality],
        )

    sessions: list[Any] = []
    skipped: list[SkippedActivity] = []
    for activity, streams in candidates:
        sport = _sport_key(activity.type)
        if sport is None or sport not in sport_modality:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id,
                    sport=activity.type,
                    reason=(
                        f"{activity.type!r} is not one of the three "
                        "intensity sports (run/bike/swim)"
                    ),
                )
            )
            continue
        # Modality selection (see step 3 of the module docstring): the
        # default map refined by the session's actual streams — power
        # first, then HR (the load engine's preference). Usability is
        # the load engine's rule: absent, empty or all-None is not
        # usable. A session with no usable modality is a named skip.
        try:
            modality = select_intensity_modality(
                sport,
                has_power_samples=has_usable_samples(streams.get("power")),
                has_hr_samples=has_usable_samples(streams.get("hr")),
            )
        except ValueError as exc:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id,
                    sport=activity.type,
                    reason=str(exc),
                )
            )
            continue
        zone_sess, reason = _build_zone_session(
            activity,
            streams,
            sport=sport,
            modality=modality,
            ftp_watts=ftp_watts,
            lthr_bpm=lthr_bpm,
            css_mps=css_mps,
            speed_tolerance_mps=speed_tolerance_mps,
            gap_cap_median_multiple=gap_cap_median_multiple,
        )
        if zone_sess is None:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id, sport=activity.type, reason=reason or ""
                )
            )
            continue
        sessions.append(zone_sess)

    weeks: tuple[WeeklyIntensity, ...] = ()
    if sessions:
        weeks = weekly_time_in_zone(sessions)

    computed_at = dt.datetime.now(dt.UTC)
    rows_upserted = 0
    no_data_rows = 0
    for week in weeks:
        for sport_result in week.sports:
            percentages = (
                list(sport_result.percentages)
                if sport_result.percentages is not None
                else None
            )
            if sport_result.status == "no_data":
                no_data_rows += 1
            await upsert_weekly_intensity(
                session,
                athlete_id=athlete_id,
                iso_year=week.iso_year,
                iso_week=week.iso_week,
                week_start=week.week_start,
                sport=sport_result.sport,
                status=sport_result.status,
                z1_seconds=sport_result.z1_seconds,
                z2_seconds=sport_result.z2_seconds,
                z3_seconds=sport_result.z3_seconds,
                total_seconds=sport_result.total_seconds,
                percentages=percentages,
                engine_version=engine_version,
                computed_at=computed_at,
            )
            rows_upserted += 1

    return IntensityReport(
        window_start=window_start,
        window_end=window_end,
        engine_version=engine_version,
        activities_considered=len(candidates),
        sessions_derived=len(sessions),
        weeks_persisted=len(weeks),
        rows_upserted=rows_upserted,
        no_data_rows=no_data_rows,
        skipped=tuple(skipped),
    )

