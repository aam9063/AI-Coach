"""Idempotent upsert repositories for the ingest schema (ING-3).

All functions use PostgreSQL ``insert().on_conflict_do_update`` keyed by the
unique constraints from §6:

- ``activity``: ``(source, source_id)`` — the idempotency anchor.
- ``activity_stream``: ``(activity_id, stream_type)`` — replaces the payload.
- ``wellness``: ``(athlete_id, date)``.
- ``daily_load``: ``(athlete_id, date, sport)`` — recomputations update the
  existing per-sport/combined rows instead of duplicating them (LOAD-10).

Functions flush but do not commit; callers own transaction boundaries.

ZON-10 additions: ``athlete_profile`` (idempotent upsert keyed by
``athlete_id``, the single-athlete integer shared with
``wellness``/``daily_load``) and ``athlete_threshold_history`` (an
APPEND-ONLY plain insert — history rows are never updated or deleted,
brief §6/§7.3).
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityRow,
    ActivityStreamRow,
    AthleteProfileRow,
    AthleteThresholdHistoryRow,
    DailyLoadRow,
    ReadinessSnapshotRow,
    SessionDurabilityRow,
    WeeklyIntensityRow,
    WellnessRow,
)


async def upsert_activity(
    session: AsyncSession,
    *,
    source: str = "intervals",
    # Real Intervals.icu ids are strings with an "i" prefix (live-verified).
    source_id: str,
    type: str = "",
    name: str = "",
    start_time: datetime,
    start_time_local: str | None = None,
    distance_m: float | None = None,
    duration_s: int | None = None,
    elevation_m: float | None = None,
    raw_file_path: str | None = None,
    # Non-authoritative cross-check value only (§5.1); never engine truth.
    intervals_icu_load: float | None = None,
    # OWNER-ENTERED INPUT (LOAD-12, §5.1): the athlete's own session RPE
    # (Intervals.icu ``icu_rpe``, scale 1-10). Unlike the cross-check value
    # above, the engine CONSUMES this (sRPE method for strength sports).
    rpe: float | None = None,
) -> ActivityRow:
    """Insert or update one activity keyed by (source, source_id)."""
    values: dict[str, Any] = {
        "source": source,
        "source_id": source_id,
        "type": type,
        "name": name,
        "start_time": start_time,
        "start_time_local": start_time_local,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "elevation_m": elevation_m,
        "raw_file_path": raw_file_path,
        "intervals_icu_load": intervals_icu_load,
        "rpe": rpe,
    }
    stmt = (
        pg_insert(ActivityRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[ActivityRow.source, ActivityRow.source_id],
            set_=values,
        )
        .returning(ActivityRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_activity_stream(
    session: AsyncSession,
    *,
    activity_id: int,
    stream_type: str,
    data: list[float],
) -> ActivityStreamRow:
    """Insert or replace one per-second stream attached to an activity."""
    values: dict[str, Any] = {
        "activity_id": activity_id,
        "stream_type": stream_type,
        "payload": data,
    }
    stmt = (
        pg_insert(ActivityStreamRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[ActivityStreamRow.activity_id, ActivityStreamRow.stream_type],
            set_=values,
        )
        .returning(ActivityStreamRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_daily_load(
    session: AsyncSession,
    *,
    athlete_id: int = 1,
    date: date,
    sport: str,
    tss: float,
    ctl: float,
    atl: float,
    tsb: float,
    methods: Mapping[str, int] | None = None,
    engine_version: str,
    computed_at: datetime,
) -> DailyLoadRow:
    """Insert or update one daily-load row keyed by (athlete_id, date, sport).

    Idempotent per the ``uq_daily_load_athlete_date_sport`` unique key:
    recomputing a window replaces the engine outputs (and the method-usage
    trace, and ``computed_at``) on the existing row. Flushes without
    committing; the caller owns the transaction.
    """
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "date": date,
        "sport": sport,
        "tss": tss,
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb,
        "methods": dict(methods) if methods is not None else None,
        "engine_version": engine_version,
        "computed_at": computed_at,
    }
    stmt = (
        pg_insert(DailyLoadRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[DailyLoadRow.athlete_id, DailyLoadRow.date, DailyLoadRow.sport],
            set_=values,
        )
        .returning(DailyLoadRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_athlete_profile(
    session: AsyncSession,
    *,
    athlete_id: int = 1,
    intervals_athlete_id: str | None = None,
    ftp_watts: float | None = None,
    ftp_source: str | None = None,
    cp_watts: float | None = None,
    cp_source: str | None = None,
    w_prime_joules: float | None = None,
    cs_mps: float | None = None,
    cs_source: str | None = None,
    d_prime_meters: float | None = None,
    css_mps: float | None = None,
    css_source: str | None = None,
    engine_version: str,
    updated_at: datetime,
) -> AthleteProfileRow:
    """Insert or update the athlete's current thresholds keyed by athlete_id.

    ZON-10 (brief §6/§7.3). Idempotent per the ``athlete_profile.athlete_id``
    unique key. Mirroring the ``upsert_wellness`` convention, passing no
    value for a metric CLEARS it on conflict — callers that want to change
    only some fields must load the current row first and pass the full set
    (:func:`app.services.athlete_profile.record_threshold_acceptance` does
    exactly that). ``*_source`` columns hold the engine's machine-readable
    source keys (ZON-3/ZON-10 provenance). Flushes without committing.
    """
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "intervals_athlete_id": intervals_athlete_id,
        "ftp_watts": ftp_watts,
        "ftp_source": ftp_source,
        "cp_watts": cp_watts,
        "cp_source": cp_source,
        "w_prime_joules": w_prime_joules,
        "cs_mps": cs_mps,
        "cs_source": cs_source,
        "d_prime_meters": d_prime_meters,
        "css_mps": css_mps,
        "css_source": css_source,
        "engine_version": engine_version,
        "updated_at": updated_at,
    }
    stmt = (
        pg_insert(AthleteProfileRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[AthleteProfileRow.athlete_id],
            set_=values,
        )
        .returning(AthleteProfileRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def append_threshold_history(
    session: AsyncSession,
    *,
    athlete_id: int = 1,
    metric: str,
    decision: str,
    proposed_value: float,
    prior_value: float | None = None,
    new_value: float | None = None,
    evidence: str = "",
    confirmed_by: str,
    source: str | None = None,
    engine_version: str,
    recorded_at: datetime,
) -> AthleteThresholdHistoryRow:
    """APPEND one decision row to ``athlete_threshold_history`` (ZON-10).

    Plain insert, never an upsert: history rows are immutable (brief §6
    "thresholds and their history", §7.3). Callers are the confirmation
    flow :mod:`app.services.athlete_profile`, which validates ``metric``,
    ``decision`` and the proposal before calling. Flushes without
    committing.
    """
    stmt = (
        pg_insert(AthleteThresholdHistoryRow)
        .values(
            athlete_id=athlete_id,
            metric=metric,
            decision=decision,
            prior_value=prior_value,
            new_value=new_value,
            proposed_value=proposed_value,
            evidence=evidence,
            confirmed_by=confirmed_by,
            source=source,
            engine_version=engine_version,
            recorded_at=recorded_at,
        )
        .returning(AthleteThresholdHistoryRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_wellness(
    session: AsyncSession,
    *,
    athlete_id: int = 1,
    date: date,
    hrv: float | None = None,
    ln_hrv: float | None = None,
    resting_hr: float | None = None,
    sleep_minutes: int | None = None,
    sleep_score: float | None = None,
    weight: float | None = None,
    intervals_icu_ctl: float | None = None,
    intervals_icu_atl: float | None = None,
) -> WellnessRow:
    """Insert or update one daily wellness record keyed by (athlete, date).

    ``intervals_icu_ctl``/``intervals_icu_atl`` are the NON-authoritative
    Intervals.icu PMC cross-check values (§5.1); passing no value clears
    them on conflict, so a day whose source no longer reports a value does
    not keep a stale one.
    """
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "date": date,
        "hrv": hrv,
        "ln_hrv": ln_hrv,
        "resting_hr": resting_hr,
        "sleep_minutes": sleep_minutes,
        "sleep_score": sleep_score,
        "weight": weight,
        "intervals_icu_ctl": intervals_icu_ctl,
        "intervals_icu_atl": intervals_icu_atl,
    }
    stmt = (
        pg_insert(WellnessRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[WellnessRow.athlete_id, WellnessRow.date],
            set_=values,
        )
        .returning(WellnessRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_readiness_snapshot(
    session: AsyncSession,
    *,
    athlete_id: int = 1,
    date: date,
    signals: Sequence[Mapping[str, Any]],
    adverse_signal_keys: Sequence[str],
    agreement_count: int,
    suggest_reduce_intensity: bool,
    suggestion: str | None,
    reasons: Sequence[str],
    tsb: float,
    tsb_very_negative_below: float,
    subjective_fatigue_reported: bool | None,
    acwr: float | None,
    engine_version: str,
    computed_at: datetime,
) -> ReadinessSnapshotRow:
    """Insert or update one readiness snapshot keyed by (athlete_id, date).

    RID-10 (brief §6/§7.4). Idempotent per
    ``uq_readiness_snapshot_athlete_date``: recomputing a day replaces the
    structured signals, the warning-rule result and the context inputs on
    the existing row. The JSONB ``signals`` list keeps the engine's stable
    :data:`app.engine.readiness.SIGNAL_KEYS` order; an
    ``insufficient_data`` signal is persisted AS insufficient (NULL
    observation fields), never substituted. Flushes without committing.
    """
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "date": date,
        "signals": [dict(signal) for signal in signals],
        "adverse_signal_keys": list(adverse_signal_keys),
        "agreement_count": agreement_count,
        "suggest_reduce_intensity": suggest_reduce_intensity,
        "suggestion": suggestion,
        "reasons": list(reasons),
        "tsb": tsb,
        "tsb_very_negative_below": tsb_very_negative_below,
        "subjective_fatigue_reported": subjective_fatigue_reported,
        "acwr": acwr,
        "engine_version": engine_version,
        "computed_at": computed_at,
    }
    stmt = (
        pg_insert(ReadinessSnapshotRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[ReadinessSnapshotRow.athlete_id, ReadinessSnapshotRow.date],
            set_=values,
        )
        .returning(ReadinessSnapshotRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_weekly_intensity(
    session: AsyncSession,
    *,
    athlete_id: int = 1,
    iso_year: int,
    iso_week: int,
    week_start: date,
    sport: str,
    status: str,
    z1_seconds: float,
    z2_seconds: float,
    z3_seconds: float,
    total_seconds: float,
    percentages: Sequence[float] | None,
    engine_version: str,
    computed_at: datetime,
) -> WeeklyIntensityRow:
    """Insert or update one weekly-intensity row keyed by (athlete_id,
    iso_year, iso_week, sport).

    RID-10 (brief §6/§7.5). Idempotent per
    ``uq_weekly_intensity_athlete_year_week_sport``: recomputing the window
    replaces the zone seconds, status and percentages of the existing row.
    ``percentages`` is the ``(z1, z2, z3)`` share triple or ``None`` for a
    ``no_data`` sport-week (or a zero-total week) — persisted as SQL NULL,
    never a fabricated split. Flushes without committing.
    """
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "iso_year": iso_year,
        "iso_week": iso_week,
        "week_start": week_start,
        "sport": sport,
        "status": status,
        "z1_seconds": z1_seconds,
        "z2_seconds": z2_seconds,
        "z3_seconds": z3_seconds,
        "total_seconds": total_seconds,
        "percentages": None if percentages is None else list(percentages),
        "engine_version": engine_version,
        "computed_at": computed_at,
    }
    stmt = (
        pg_insert(WeeklyIntensityRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                WeeklyIntensityRow.athlete_id,
                WeeklyIntensityRow.iso_year,
                WeeklyIntensityRow.iso_week,
                WeeklyIntensityRow.sport,
            ],
            set_=values,
        )
        .returning(WeeklyIntensityRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def upsert_session_durability(
    session: AsyncSession,
    *,
    activity_id: int,
    sport: str,
    status: str,
    ef_first_half: float | None,
    ef_second_half: float | None,
    decoupling: float | None,
    decoupling_pct: float | None,
    within_reference_band: bool | None,
    reference_band: float,
    intensity_first_half: float | None,
    intensity_second_half: float | None,
    intensity_drift: float,
    max_intensity_drift: float,
    n_samples: int,
    n_first_half: int,
    n_second_half: int,
    detail: str,
    engine_version: str,
    computed_at: datetime,
) -> SessionDurabilityRow:
    """Insert or update one session-durability row keyed by activity_id.

    RID-10 (brief §6/§7.6). Idempotent per
    ``uq_session_durability_activity``: recomputing the window replaces
    the EF/decoupling evidence on the existing row. A ``not_steady``
    result is persisted AS not steady (NULL EF/decoupling, the measured
    drift and the engine detail), never with a fabricated value. Flushes
    without committing.
    """
    values: dict[str, Any] = {
        "activity_id": activity_id,
        "sport": sport,
        "status": status,
        "ef_first_half": ef_first_half,
        "ef_second_half": ef_second_half,
        "decoupling": decoupling,
        "decoupling_pct": decoupling_pct,
        "within_reference_band": within_reference_band,
        "reference_band": reference_band,
        "intensity_first_half": intensity_first_half,
        "intensity_second_half": intensity_second_half,
        "intensity_drift": intensity_drift,
        "max_intensity_drift": max_intensity_drift,
        "n_samples": n_samples,
        "n_first_half": n_first_half,
        "n_second_half": n_second_half,
        "detail": detail,
        "engine_version": engine_version,
        "computed_at": computed_at,
    }
    stmt = (
        pg_insert(SessionDurabilityRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[SessionDurabilityRow.activity_id],
            set_=values,
        )
        .returning(SessionDurabilityRow)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()
