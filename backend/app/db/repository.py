"""Idempotent upsert repositories for the ingest schema (ING-3).

All functions use PostgreSQL ``insert().on_conflict_do_update`` keyed by the
unique constraints from §6:

- ``activity``: ``(source, source_id)`` — the idempotency anchor.
- ``activity_stream``: ``(activity_id, stream_type)`` — replaces the payload.
- ``wellness``: ``(athlete_id, date)``.

Functions flush but do not commit; callers own transaction boundaries.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRow, ActivityStreamRow, WellnessRow


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
) -> WellnessRow:
    """Insert or update one daily wellness record keyed by (athlete, date)."""
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "date": date,
        "hrv": hrv,
        "ln_hrv": ln_hrv,
        "resting_hr": resting_hr,
        "sleep_minutes": sleep_minutes,
        "sleep_score": sleep_score,
        "weight": weight,
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
