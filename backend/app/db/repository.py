"""Idempotent upsert repositories for the ingest schema (ING-3).

All functions use PostgreSQL ``insert().on_conflict_do_update`` keyed by the
unique constraints from §6:

- ``activity``: ``(source, source_id)`` — the idempotency anchor.
- ``activity_stream``: ``(activity_id, stream_type)`` — replaces the payload.
- ``wellness``: ``(athlete_id, date)``.
- ``daily_load``: ``(athlete_id, date, sport)`` — recomputations update the
  existing per-sport/combined rows instead of duplicating them (LOAD-10).

Functions flush but do not commit; callers own transaction boundaries.
"""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRow, ActivityStreamRow, DailyLoadRow, WellnessRow


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
