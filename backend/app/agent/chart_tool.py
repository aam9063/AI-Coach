"""Data assembly for the ``plot_metric`` tool (CH-10, PROJECT_BRIEF §9.3).

The tool wrapper lives in :mod:`app.agent.tools` (argument validation,
the kind registry, save + media URL); THIS module holds what the wrapper
delegates: the typed result model and the per-kind assembly of the
MINIMUM renderer inputs (§14) from the persisted engine rows. It imports
only the pure renderers, the DB models and the readiness signal key —
never :mod:`app.agent.tools` (no import cycle).

Kind -> renderer + data source (the registry the tool exposes):

- ``performance_manager`` (alias ``ctl_atl``): the ``daily_load``
  COMBINED rows (the all-sports aggregate) ->
  :func:`app.charts.render_performance_manager`. A sport filter is
  IGNORED: the performance manager is by definition the combined view.
- ``weekly_tss``: the per-sport ``daily_load`` rows, TSS summed per ISO
  week -> :func:`app.charts.render_weekly_tss_by_sport`. A sport filter
  narrows the rows to that sport.
- ``time_in_zone`` (alias ``intensity``): the ``weekly_intensity`` rows
  WITH zone data (``status == "data"``; seconds -> minutes) ->
  :func:`app.charts.render_weekly_time_in_zone``. A sport filter narrows
  the rows; ``no_data`` weeks are reported AS missing, never drawn.
- ``hrv``: the daily ln(rMSSD) from the ``wellness`` rows plus the
  per-day baseline band from the persisted readiness snapshot's HRV
  signal (its own stored ``band_half_width_sd``) ->
  :func:`app.charts.render_hrv_trend`. A sport filter is IGNORED (HRV is
  sport-agnostic). A day needs BOTH the daily value and a baseline band
  to be drawn — a day with only one of the two is truthfully excluded
  and counted in ``coverage``.
- ``race_prediction``: Feature 8's distance path (RP-10,
  :mod:`app.agent.race_tool`) rendered as the prediction breakdown
  (CH-8). The RACE DISTANCE KEY travels in ``sport`` (required:
  sprint/olympic/half/full) and ``date_range`` is IGNORED — a prediction
  is about a race distance, not a trailing window.

Kinds whose renderer exists but whose engine data source does not yet
(mean-maximal curve, run pace/HR + EF trends) are rejected by the tool
with the explicit :data:`UNAVAILABLE_CHART_KINDS` detail — nothing is
ever fabricated.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import race_tool
from app.charts import (
    HrvSample,
    PerformanceManagerSample,
    RacePredictionSample,
    WeeklyTssSample,
    WeeklyZoneSample,
    render_hrv_trend,
    render_performance_manager,
    render_race_prediction_breakdown,
    render_weekly_time_in_zone,
    render_weekly_tss_by_sport,
)
from app.core.settings import Settings
from app.db.models import (
    DailyLoadRow,
    ReadinessSnapshotRow,
    WeeklyIntensityRow,
    WellnessRow,
)
from app.engine.readiness import DEFAULT_HRV_BAND_SD, SIGNAL_KEY_HRV

SUPPORTED_CHART_KINDS: Final[tuple[str, ...]] = (
    "performance_manager",
    "weekly_tss",
    "time_in_zone",
    "hrv",
    "race_prediction",
)
"""The five kinds ``plot_metric`` renders (ODD CH-10 + RP-10): each maps
to ONE canonical kind string, one renderer and one data assembly."""

CHART_KIND_ALIASES: Final[dict[str, str]] = {
    "ctl_atl": "performance_manager",
    "intensity": "time_in_zone",
}
"""The stub docstring's names, kept working as aliases: each maps to
exactly ONE canonical kind in :data:`SUPPORTED_CHART_KINDS`."""

UNAVAILABLE_CHART_KINDS: Final[dict[str, str]] = {
    "mean_maximal_curve": (
        "the mean-maximal power curve chart has no engine data source yet: "
        "mean-maximal efforts and the CP fit are not persisted by the engine, "
        "so there is nothing truthful to draw — never fabricate a curve."
    ),
    "run_trends": (
        "the run pace/HR and EF trend charts have no engine data source yet: "
        "the per-session trend assembly over session_durability is not built, "
        "so there is nothing truthful to draw — never fabricate a trend."
    ),
    "ef_trend": (
        "the run pace/HR and EF trend charts have no engine data source yet: "
        "the per-session trend assembly over session_durability is not built, "
        "so there is nothing truthful to draw — never fabricate a trend."
    ),
    "run_pace_hr": (
        "the run pace/HR and EF trend charts have no engine data source yet: "
        "the per-session trend assembly over session_durability is not built, "
        "so there is nothing truthful to draw — never fabricate a trend."
    ),
}
"""Kinds the renderers can draw but the engine cannot source yet: the
tool answers with THIS detail verbatim instead of a chart."""

CHART_RENDERERS: Final[dict[str, Callable[[Sequence[Any]], bytes]]] = {
    "performance_manager": render_performance_manager,
    "weekly_tss": render_weekly_tss_by_sport,
    "time_in_zone": render_weekly_time_in_zone,
    "hrv": render_hrv_trend,
    "race_prediction": render_race_prediction_breakdown,
}


class ChartCoverage(BaseModel):
    """How much data ACTUALLY backs a chart — truthful, never padded.

    ``samples_in_chart`` is what was drawn; when the window holds fewer
    engine rows than the window has days/weeks, the smaller number IS the
    answer (the agent can say what the chart is based on, §9.3)."""

    window_days: int
    window_start: str
    window_end: str
    samples_in_chart: int
    sport: str | None = None
    days_with_hrv_value: int | None = None
    """HRV kind only: wellness days with a ln(rMSSD) value in the window
    (a superset of ``samples_in_chart`` — some lacked a baseline band)."""


class PlotMetricResult(BaseModel):
    """The typed ``plot_metric`` result (CH-10): chart id + public media
    URL plus the WA-6 provenance contract (``engine_version``,
    ``computed_at``, ``coverage``)."""

    status: Literal["ok", "insufficient_data", "error"]
    kind: str
    engine_version: str
    computed_at: str
    detail: str | None = None
    chart_id: int | None = None
    media_url: str | None = None
    coverage: ChartCoverage | None = None


@dataclass(frozen=True)
class ChartAssembly:
    """What one assembler produced for the tool wrapper."""

    kind: str
    samples: Sequence[Any]
    provenance_row: Any | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    missing_data_detail: str = ""


Assembler = Callable[
    [AsyncSession, date, date, str | None, Settings], Awaitable[ChartAssembly]
]

# --- per-kind assemblers (DB reads OUTSIDE the pure renderers, §6) --------


def _week_start(day: date) -> date:
    """The Monday of ``day``'s ISO week."""
    iso_year, iso_week, _ = day.isocalendar()
    return date.fromisocalendar(iso_year, iso_week, 1)


async def _assemble_performance_manager(
    session: AsyncSession,
    start: date,
    end: date,
    sport: str | None,
    settings: Settings,
) -> ChartAssembly:
    """The ``daily_load`` COMBINED rows: CTL/ATL/TSB per day, all sports.

    ``sport`` is deliberately ignored — the performance manager aggregates
    by definition (documented in the tool docstring)."""
    del sport  # documented: ignored for the combined view
    rows = (
        (
            await session.execute(
                select(DailyLoadRow)
                .where(
                    DailyLoadRow.athlete_id == 1,
                    DailyLoadRow.sport == "combined",
                    DailyLoadRow.date >= start,
                    DailyLoadRow.date <= end,
                )
                .order_by(DailyLoadRow.date)
            )
        )
        .scalars()
        .all()
    )
    samples = [
        PerformanceManagerSample(day=row.date, ctl=row.ctl, atl=row.atl, tsb=row.tsb)
        for row in rows
    ]
    return ChartAssembly(
        kind="performance_manager",
        samples=samples,
        provenance_row=rows[-1] if rows else None,
        coverage={"sport": None},
        missing_data_detail=(
            f"no daily_load combined rows between {start.isoformat()} and "
            f"{end.isoformat()}: the engine has not computed training load for "
            "this window. Connect your device so activities sync to "
            "Intervals.icu, then run `python -m app.db.daily_load` to compute "
            "CTL/ATL/TSB from the synced activities."
        ),
    )


async def _assemble_weekly_tss(
    session: AsyncSession,
    start: date,
    end: date,
    sport: str | None,
    settings: Settings,
) -> ChartAssembly:
    """Per-sport ``daily_load`` rows, TSS summed per ISO week (the
    ``combined`` rows are excluded — they would double the stack)."""
    query = select(DailyLoadRow).where(
        DailyLoadRow.athlete_id == 1,
        DailyLoadRow.sport != "combined",
        DailyLoadRow.date >= start,
        DailyLoadRow.date <= end,
    )
    if sport:
        query = query.where(DailyLoadRow.sport == sport)
    rows = (await session.execute(query.order_by(DailyLoadRow.date))).scalars().all()

    tss_by_week: dict[date, dict[str, float]] = {}
    for row in rows:
        week_tss = tss_by_week.setdefault(_week_start(row.date), {})
        week_tss[row.sport] = week_tss.get(row.sport, 0.0) + row.tss
    samples = [
        WeeklyTssSample(week_start=week, sport_tss=tuple(sorted(per_sport.items())))
        for week, per_sport in sorted(tss_by_week.items())
    ]
    return ChartAssembly(
        kind="weekly_tss",
        samples=samples,
        provenance_row=rows[-1] if rows else None,
        coverage={"sport": sport},
        missing_data_detail=(
            f"no per-sport daily_load rows between {start.isoformat()} and "
            f"{end.isoformat()}: the engine has not computed training load for "
            "this window. Connect your device so activities sync to "
            "Intervals.icu, then run `python -m app.db.daily_load`."
        ),
    )


async def _assemble_time_in_zone(
    session: AsyncSession,
    start: date,
    end: date,
    sport: str | None,
    settings: Settings,
) -> ChartAssembly:
    """``weekly_intensity`` rows WITH zone data (``status == "data"``),
    seconds -> minutes; ``no_data`` weeks are excluded — a missing week is
    reported AS missing, never drawn as a zero bar (§7.5)."""
    query = select(WeeklyIntensityRow).where(
        WeeklyIntensityRow.athlete_id == 1,
        WeeklyIntensityRow.status == "data",
        WeeklyIntensityRow.week_start >= start,
        WeeklyIntensityRow.week_start <= end,
    )
    if sport:
        query = query.where(WeeklyIntensityRow.sport == sport)
    rows = (
        await session.execute(query.order_by(WeeklyIntensityRow.week_start))
    ).scalars().all()

    zones_by_week: dict[date, dict[str, tuple[float, float, float]]] = {}
    for row in rows:
        week_zones = zones_by_week.setdefault(row.week_start, {})
        week_zones[row.sport] = (
            row.z1_seconds / 60.0,
            row.z2_seconds / 60.0,
            row.z3_seconds / 60.0,
        )
    samples = [
        WeeklyZoneSample(week_start=week, sport_zones=tuple(sorted(per_sport.items())))
        for week, per_sport in sorted(zones_by_week.items())
    ]
    return ChartAssembly(
        kind="time_in_zone",
        samples=samples,
        provenance_row=rows[-1] if rows else None,
        coverage={"sport": sport},
        missing_data_detail=(
            f"no weekly_intensity rows with zone data between {start.isoformat()} "
            f"and {end.isoformat()}: the engine has not classified sessions into "
            "zones for this window. Connect your device so activities sync to "
            "Intervals.icu, then run `python -m app.db.engine_outputs`."
        ),
    )


async def _assemble_hrv(
    session: AsyncSession,
    start: date,
    end: date,
    sport: str | None,
    settings: Settings,
) -> ChartAssembly:
    """Daily ln(rMSSD) from ``wellness`` plus the readiness snapshot's
    per-day HRV baseline band (its own stored ``band_half_width_sd``).

    ``sport`` is deliberately ignored — HRV is sport-agnostic (documented
    in the tool docstring). A day is drawn only when BOTH the daily value
    and a baseline band exist; the coverage reports what was excluded."""
    del sport  # documented: ignored, HRV is sport-agnostic
    wellness = (
        (
            await session.execute(
                select(WellnessRow)
                .where(
                    WellnessRow.athlete_id == 1,
                    WellnessRow.date >= start,
                    WellnessRow.date <= end,
                )
                .order_by(WellnessRow.date)
            )
        )
        .scalars()
        .all()
    )
    ln_by_day = {row.date: _ln_hrv_value(row) for row in wellness}

    snapshots = (
        (
            await session.execute(
                select(ReadinessSnapshotRow)
                .where(
                    ReadinessSnapshotRow.athlete_id == 1,
                    ReadinessSnapshotRow.date >= start,
                    ReadinessSnapshotRow.date <= end,
                )
                .order_by(ReadinessSnapshotRow.date)
            )
        )
        .scalars()
        .all()
    )
    band_by_day: dict[date, tuple[float, float]] = {}
    for snapshot in snapshots:
        signal = next(
            (s for s in snapshot.signals if s.get("key") == SIGNAL_KEY_HRV), None
        )
        if signal is None:
            continue
        baseline_mean = signal.get("baseline_mean")
        baseline_sd = signal.get("baseline_sd")
        if baseline_mean is None or baseline_sd is None:
            continue  # insufficient_data signal: the band is reported AS missing
        half = float(signal.get("band_half_width_sd") or DEFAULT_HRV_BAND_SD) * float(
            baseline_sd
        )
        band_by_day[snapshot.date] = (baseline_mean - half, baseline_mean + half)

    samples = [
        HrvSample(
            day=day,
            ln_rmssd=value,
            baseline_lower=band_by_day[day][0],
            baseline_upper=band_by_day[day][1],
        )
        for day, value in sorted(ln_by_day.items())
        if value is not None and day in band_by_day
    ]
    latest_snapshot = snapshots[-1] if snapshots else None
    return ChartAssembly(
        kind="hrv",
        samples=samples,
        provenance_row=latest_snapshot,
        coverage={
            "sport": None,
            "days_with_hrv_value": sum(1 for v in ln_by_day.values() if v is not None),
        },
        missing_data_detail=(
            f"no HRV data to chart between {start.isoformat()} and "
            f"{end.isoformat()}: the chart needs daily wellness ln(rMSSD) values "
            "AND the readiness engine's baseline band for those days. Connect a "
            "device that syncs HRV to Intervals.icu, then run "
            "`python -m app.db.daily_load` and `python -m app.db.engine_outputs`."
        ),
    )


def _ln_hrv_value(row: WellnessRow) -> float | None:
    """The daily ln(rMSSD): the stored ``ln_hrv``, or ``ln(hrv)`` when
    only the rMSSD was stored — the SAME convention as
    :mod:`app.services.readiness` (a non-positive rMSSD is a missing
    measurement, never an input to the chart)."""
    if row.ln_hrv is not None:
        return row.ln_hrv
    if row.hrv is not None and row.hrv > 0.0:
        return math.log(row.hrv)
    return None


async def _assemble_race_prediction(
    session: AsyncSession,
    start: date,
    end: date,
    sport: str | None,
    settings: Settings,
) -> ChartAssembly:
    """Feature 8's distance path (RP-10) rendered as the prediction
    breakdown chart (CH-8): one bar per segment with its uncertainty
    interval.

    ``sport`` carries the RACE DISTANCE KEY (``sprint``, ``olympic``,
    ``half``, ``full``) — NOT an engine sport — already validated by the
    tool wrapper. ``date_range`` is IGNORED: a prediction is about a
    race distance, not a trailing window. The distance path never
    touches the database (documented in :mod:`app.agent.race_tool`), so
    ``session``/``start``/``end`` are correspondingly unused."""
    result = await race_tool.predict_race(
        session,
        race_id=None,
        distance=sport or "",
        target_date=None,
        settings=settings,
        computed_at=datetime.now(UTC),
    )
    if result.status != "ok" or result.segments is None:
        return ChartAssembly(
            kind="race_prediction",
            samples=(),
            missing_data_detail=result.detail or "the race prediction is unavailable",
        )
    samples = tuple(
        RacePredictionSample(
            label=segment.segment,
            point_seconds=segment.point,
            lower_seconds=segment.lower,
            upper_seconds=segment.upper,
        )
        for segment in result.segments
    )
    return ChartAssembly(kind="race_prediction", samples=samples)


_ASSEMBLERS: Final[dict[str, Assembler]] = {
    "performance_manager": _assemble_performance_manager,
    "weekly_tss": _assemble_weekly_tss,
    "time_in_zone": _assemble_time_in_zone,
    "hrv": _assemble_hrv,
    "race_prediction": _assemble_race_prediction,
}


async def assemble_chart_samples(
    session: AsyncSession,
    kind: str,
    start: date,
    end: date,
    sport: str | None,
    settings: Settings,
) -> ChartAssembly:
    """Assemble the renderer inputs for ``kind`` over the inclusive
    ``[start, end]`` window (dispatch through :data:`_ASSEMBLERS`).

    ``settings`` is the tool's resolved settings — the
    ``race_prediction`` assembler reads the athlete inputs from it; the
    DB-backed assemblers ignore it."""
    assembler = _ASSEMBLERS[kind]
    return await assembler(session, start, end, sport, settings)
