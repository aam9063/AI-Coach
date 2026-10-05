"""Daily-load persistence service (LOAD-10, first half) — the §6 integration layer.

This module is the thin, typed bridge between the ingested data in Postgres
and the pure engine (``app.engine.load`` / ``app.engine.pmc``). It lives
OUTSIDE ``app/engine`` because it does I/O: the engine never imports DB or
service code (§6 purity rule, enforced by the engine purity test).

Pipeline of :func:`recompute_daily_load` over a trailing window of
``days`` calendar days ending at ``window_end``:

1. Read the athlete's activities whose activity date (see
   :func:`_activity_date`) falls inside the window, together with their
   stored per-second streams (``activity_stream`` JSONB payloads).
2. Derive pure-engine inputs per activity: the average HR is the mean of
   the valid (non-``None``) ``hr`` samples — ``None`` marks a stream gap
   and is excluded, mirroring the engine's gap semantics — the duration
   comes from ``activity.duration_s``, and power/speed/distance/altitude
   streams are passed through where present.
3. Call :func:`app.engine.load.select_load_method` with the caller-supplied
   thresholds (built from settings by the CLI; §14 owner configuration) and
   TRIMP coefficients, honoring the fixed method order power ->
   pace/speed -> HR -> sRPE.
4. Aggregate the chosen TSS per (day, sport) — the sport key is the
   engine-normalized lower-case activity type (e.g. ``"ride"``) — and run
   :func:`app.engine.pmc.compute_pmc_per_sport` over the contiguous window
   (rest days as explicit ``0.0``) to get per-sport and combined
   CTL/ATL/TSB.
5. Upsert one ``daily_load`` row per (athlete, date, sport) plus one
   ``sport="combined"`` row per day, each stamped with ``engine_version``
   (§6) and ``computed_at``.

Skipped/undecidable contract (never silent): an activity that cannot
contribute load — missing/non-positive duration, an unknown sport, or no
applicable load method (e.g. no HR stream and no RPE) — is reported in
:attr:`DailyLoadReport.skipped` with its id and the concrete reason, and
contributes no load. Nothing is ever dropped silently; a window with no
usable activity at all persists no rows and says so in the report.

All writes go through :func:`app.db.repository.upsert_daily_load`
(idempotent per ``(athlete_id, date, sport)``) and flush without
committing; the caller owns the transaction boundary.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRow, ActivityStreamRow
from app.db.repository import upsert_daily_load
from app.engine.load import (
    ActivityLoadInput,
    ThresholdBundle,
    TrimpCoefficients,
    select_load_method,
)
from app.engine.pmc import compute_pmc_per_sport

__all__ = [
    "COMBINED_SPORT_KEY",
    "DailyLoadReport",
    "SkippedActivity",
    "average_hr_bpm",
    "build_activity_load_input",
    "recompute_daily_load",
]

COMBINED_SPORT_KEY = "combined"
"""Sport key of the all-sports daily_load row (one per day, next to the
per-sport rows; the natural shape given ``compute_pmc_per_sport``)."""

_STREAM_ALIASES: Mapping[str, tuple[str, ...]] = {
    "power": ("power",),
    "speed": ("speed",),
    "distance": ("distance", "dist"),
    "altitude": ("altitude", "alt"),
    "hr": ("hr",),
}
"""Engine input -> stored ``activity_stream.stream_type`` names.

Intervals.icu names its streams ``hr``/``power``/``speed``/``cadence`` and
uses the short forms ``alt``/``dist`` for altitude and distance; both
spellings are accepted. Unknown stream types are ignored (the engine only
consumes these inputs).
"""


@dataclass(frozen=True)
class SkippedActivity:
    """One activity that contributed no load, with the concrete reason.

    Reporting is the contract: nothing is ever dropped silently (LOAD-10).
    """

    activity_id: int
    sport: str
    reason: str


@dataclass(frozen=True)
class DailyLoadReport:
    """Outcome of one :func:`recompute_daily_load` run (human + test facing).

    ``rows_upserted`` counts persisted ``daily_load`` rows (per-sport plus
    combined rows); ``per_sport_rows`` breaks that down per sport key.
    ``per_sport_activities`` counts the activities that contributed load per
    sport, ``per_day_activities`` per calendar day. ``skipped`` lists every
    considered-but-undecidable activity with its reason.
    """

    window_start: dt.date
    window_end: dt.date
    engine_version: str
    activities_considered: int
    rows_upserted: int
    per_day_activities: dict[dt.date, int] = field(default_factory=dict)
    per_sport_rows: dict[str, int] = field(default_factory=dict)
    per_sport_activities: dict[str, int] = field(default_factory=dict)
    skipped: tuple[SkippedActivity, ...] = ()


def average_hr_bpm(samples: Sequence[float | None] | None) -> float | None:
    """Average HR (bpm): mean of the valid (non-``None``) stream samples.

    ``None`` entries mark per-second stream gaps (ingest parser semantics)
    and are excluded from the mean — never counted as 0 bpm. An absent
    stream, an empty payload, or a payload with no valid sample yields
    ``None`` ("no average HR recorded"), which the engine's method
    selection then reports as a skip reason instead of inventing a value.
    """
    if samples is None:
        return None
    valid = [s for s in samples if s is not None]
    if not valid:
        return None
    return math.fsum(valid) / len(valid)


def build_activity_load_input(
    activity: ActivityRow,
    streams: Mapping[str, Sequence[float | None]],
) -> tuple[ActivityLoadInput | None, str | None]:
    """Derive the pure-engine input for one stored activity.

    Returns ``(input, None)`` on success, or ``(None, reason)`` when the
    activity is undecidable before the engine is even called (currently:
    missing or non-positive ``duration_s``). The average HR comes from the
    ``hr`` payload via :func:`average_hr_bpm`; power/speed/distance/
    altitude streams are passed through where present (see
    :data:`_STREAM_ALIASES` for the stored stream-type names). RPE is not
    stored yet, so ``rpe`` stays ``None`` (the sRPE method is then reported
    as skipped by the engine, never guessed).
    """
    if activity.duration_s is None or activity.duration_s <= 0:
        return None, (
            f"missing or non-positive duration_s ({activity.duration_s!r})"
        )

    def _stream(names: tuple[str, ...]) -> list[float | None] | None:
        for name in names:
            if name in streams:
                return list(streams[name])
        return None

    return (
        ActivityLoadInput(
            sport=activity.type,
            duration_s=float(activity.duration_s),
            power_samples=_stream(_STREAM_ALIASES["power"]),
            speed_samples=_stream(_STREAM_ALIASES["speed"]),
            distance_samples=_stream(_STREAM_ALIASES["distance"]),
            altitude_samples=_stream(_STREAM_ALIASES["altitude"]),
            hr_avg_bpm=average_hr_bpm(_stream(_STREAM_ALIASES["hr"])),
            rpe=None,
        ),
        None,
    )


def _activity_date(activity: ActivityRow) -> dt.date:
    """Calendar day an activity's load belongs to.

    The local start date when ``start_time_local`` parses (daily load is a
    local-time concept: a 23:30 local ride belongs to that local day); the
    UTC start date otherwise. An unparseable local string is never fatal —
    it just falls back to the UTC date.
    """
    if activity.start_time_local:
        try:
            return dt.datetime.fromisoformat(activity.start_time_local).date()
        except ValueError:
            pass
    return activity.start_time.date()


async def _load_window_activities(
    session: AsyncSession, *, window_start: dt.date, window_end: dt.date
) -> list[tuple[ActivityRow, dict[str, list[Any]]]]:
    """Read window activities with their streams (athlete 1 schema today).

    The SQL pre-filter brackets the window by a day of margin (local-vs-UTC
    offset safety) and the exact :func:`_activity_date` filter is applied in
    Python. Streams are fetched once and grouped per activity.
    """
    margin = dt.timedelta(days=1)
    stmt = select(ActivityRow).where(
        ActivityRow.start_time
        >= dt.datetime.combine(window_start - margin, dt.time.min, tzinfo=dt.UTC),
        ActivityRow.start_time
        < dt.datetime.combine(window_end + margin, dt.time.min, tzinfo=dt.UTC),
    )
    activities = (await session.execute(stmt)).scalars().all()
    in_window = [a for a in activities if window_start <= _activity_date(a) <= window_end]
    if not in_window:
        return []

    stream_rows = (
        (
            await session.execute(
                select(ActivityStreamRow).where(
                    ActivityStreamRow.activity_id.in_([a.id for a in in_window])
                )
            )
        )
        .scalars()
        .all()
    )
    streams_by_activity: dict[int, dict[str, list[Any]]] = {}
    for row in stream_rows:
        streams_by_activity.setdefault(row.activity_id, {})[row.stream_type] = row.payload
    return [(a, streams_by_activity.get(a.id, {})) for a in in_window]


async def recompute_daily_load(
    session: AsyncSession,
    *,
    window_end: dt.date,
    days: int,
    thresholds: ThresholdBundle,
    coefficients: TrimpCoefficients,
    engine_version: str,
    athlete_id: int = 1,
) -> DailyLoadReport:
    """Recompute and persist ``daily_load`` rows for a trailing window.

    See the module docstring for the pipeline and the skipped/undecidable
    contract. ``days`` must be positive; the window is the ``days``
    calendar days ending at (and including) ``window_end``. The PMC
    recursions start from zero seeds at ``window_start`` — continuing prior
    history (explicit seeds) belongs to a later incremental recompute.

    Upserts are idempotent per ``(athlete_id, date, sport)``; the function
    flushes without committing.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days!r}")
    window_start = window_end - dt.timedelta(days=days - 1)
    window_dates = [window_start + dt.timedelta(days=offset) for offset in range(days)]

    candidates = await _load_window_activities(
        session, window_start=window_start, window_end=window_end
    )

    sport_loads: dict[str, dict[dt.date, float]] = {}
    method_usage: dict[tuple[str, dt.date], Counter[str]] = {}
    per_day_activities: Counter[dt.date] = Counter()
    per_sport_activities: Counter[str] = Counter()
    skipped: list[SkippedActivity] = []

    for activity, streams in candidates:
        load_input, pre_reason = build_activity_load_input(activity, streams)
        if load_input is None or pre_reason is not None:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id, sport=activity.type, reason=pre_reason or ""
                )
            )
            continue
        try:
            selection = select_load_method(load_input, thresholds, coefficients=coefficients)
        except ValueError as exc:
            # Undecidable: the engine lists every rejected method and why.
            skipped.append(
                SkippedActivity(activity_id=activity.id, sport=activity.type, reason=str(exc))
            )
            continue
        sport = activity.type.strip().lower()  # engine-normalized key
        day = _activity_date(activity)
        sport_loads.setdefault(sport, {})[day] = (
            sport_loads.get(sport, {}).get(day, 0.0) + selection.tss
        )
        method_usage.setdefault((sport, day), Counter())[selection.method] += 1
        per_day_activities[day] += 1
        per_sport_activities[sport] += 1

    rows_upserted = 0
    per_sport_rows: dict[str, int] = {}
    if sport_loads:
        computed_at = dt.datetime.now(dt.UTC)
        per_sport = compute_pmc_per_sport(
            {
                sport: {date: series.get(date, 0.0) for date in window_dates}
                for sport, series in sport_loads.items()
            }
        )
        for sport, series in per_sport.per_sport.items():
            for pmc_day in series.days:
                usage = method_usage.get((sport, pmc_day.date))
                await upsert_daily_load(
                    session,
                    athlete_id=athlete_id,
                    date=pmc_day.date,
                    sport=sport,
                    tss=pmc_day.tss,
                    ctl=pmc_day.ctl,
                    atl=pmc_day.atl,
                    tsb=pmc_day.tsb,
                    methods=dict(usage) if usage else None,
                    engine_version=engine_version,
                    computed_at=computed_at,
                )
                rows_upserted += 1
            per_sport_rows[sport] = len(series.days)
        for pmc_day in per_sport.combined.days:
            merged_usage: Counter[str] = Counter()
            for sport in per_sport.per_sport:
                sport_usage = method_usage.get((sport, pmc_day.date))
                if sport_usage is not None:
                    merged_usage.update(sport_usage)
            await upsert_daily_load(
                session,
                athlete_id=athlete_id,
                date=pmc_day.date,
                sport=COMBINED_SPORT_KEY,
                tss=pmc_day.tss,
                ctl=pmc_day.ctl,
                atl=pmc_day.atl,
                tsb=pmc_day.tsb,
                methods=dict(merged_usage) if merged_usage else None,
                engine_version=engine_version,
                computed_at=computed_at,
            )
            rows_upserted += 1
        per_sport_rows[COMBINED_SPORT_KEY] = len(per_sport.combined.days)

    return DailyLoadReport(
        window_start=window_start,
        window_end=window_end,
        engine_version=engine_version,
        activities_considered=len(candidates),
        rows_upserted=rows_upserted,
        per_day_activities=dict(sorted(per_day_activities.items())),
        per_sport_rows=dict(sorted(per_sport_rows.items())),
        per_sport_activities=dict(sorted(per_sport_activities.items())),
        skipped=tuple(skipped),
    )
