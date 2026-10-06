"""ING-3 RED: model round-trip tests for activity / activity_stream / wellness.

Schema per PROJECT_BRIEF.md §6 data model:
- ``activity`` carries the (source, source_id) unique key used for idempotent
  ingestion, UTC start time, local start time, summary fields the Intervals.icu
  API provides, a nullable raw file path, and a clearly non-authoritative
  cross-check field for Intervals-side load metrics.
- ``activity_stream`` holds per-second arrays (JSONB payload) linked to the
  activity, unique per (activity, stream type).
- ``wellness`` is keyed by a (athlete, date) unique constraint.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import ActivityRow, ActivityStreamRow, WellnessRow

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 2, 1, 8, 30, tzinfo=UTC)


def _activity(source_id: str, name: str = "Morning ride") -> ActivityRow:
    return ActivityRow(
        source="intervals",
        source_id=source_id,
        type="Ride",
        name=name,
        start_time=_T0,
        start_time_local="2026-02-01T09:30:00+01:00",
        distance_m=42500.0,
        duration_s=5400,
        elevation_m=320.0,
        intervals_icu_load=142.5,
    )


class TestActivity:
    async def test_roundtrip(self, db_session):  # type: ignore[no-untyped-def]
        db_session.add(_activity("i163428838"))
        await db_session.flush()

        stored = (
            await db_session.execute(
                select(ActivityRow).where(ActivityRow.source_id == "i163428838")
            )
        ).scalar_one()

        assert stored.id is not None
        assert stored.source == "intervals"
        assert stored.type == "Ride"
        assert stored.name == "Morning ride"
        assert stored.start_time == _T0
        assert stored.start_time.tzinfo is not None  # UTC-aware
        assert stored.start_time_local == "2026-02-01T09:30:00+01:00"
        assert stored.distance_m == 42500.0
        assert stored.duration_s == 5400
        assert stored.elevation_m == 320.0
        # Nullable raw FIT file path (ING-6 stores the actual path later).
        assert stored.raw_file_path is None
        # Non-authoritative cross-check field: Intervals-side load only.
        assert stored.intervals_icu_load == 142.5

    async def test_source_id_is_unique_for_idempotency(self, db_session):  # type: ignore[no-untyped-def]
        db_session.add(_activity("i163428838"))
        await db_session.flush()
        db_session.add(_activity("i163428838", name="Duplicate"))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestActivityStream:
    async def test_roundtrip_linked_to_activity(self, db_session):  # type: ignore[no-untyped-def]
        activity = _activity("i163419945")
        db_session.add(activity)
        await db_session.flush()
        db_session.add(
            ActivityStreamRow(
                activity_id=activity.id,
                stream_type="power",
                payload=[0.0, 100.0, 250.0],
            )
        )
        await db_session.flush()

        stored = (
            await db_session.execute(select(ActivityStreamRow))
        ).scalar_one()
        assert stored.activity_id == activity.id
        assert stored.stream_type == "power"
        assert stored.payload == [0.0, 100.0, 250.0]

    async def test_stream_type_unique_per_activity(self, db_session):  # type: ignore[no-untyped-def]
        activity = _activity("i163419946")
        db_session.add(activity)
        await db_session.flush()
        db_session.add(ActivityStreamRow(activity_id=activity.id, stream_type="hr", payload=[60]))
        await db_session.flush()
        db_session.add(
            ActivityStreamRow(activity_id=activity.id, stream_type="hr", payload=[61])
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestWellness:
    async def test_roundtrip(self, db_session):  # type: ignore[no-untyped-def]
        db_session.add(
            WellnessRow(
                athlete_id=1,
                date=date(2026, 2, 1),
                hrv=58.0,
                ln_hrv=4.06,
                resting_hr=48.0,
                sleep_minutes=450,
                sleep_score=82.0,
                weight=70.5,
            )
        )
        await db_session.flush()

        stored = (await db_session.execute(select(WellnessRow))).scalar_one()
        assert stored.athlete_id == 1
        assert stored.date == date(2026, 2, 1)
        assert stored.hrv == 58.0
        assert stored.ln_hrv == 4.06
        assert stored.resting_hr == 48.0
        assert stored.sleep_minutes == 450
        assert stored.sleep_score == 82.0
        assert stored.weight == 70.5

    async def test_athlete_date_is_unique(self, db_session):  # type: ignore[no-untyped-def]
        db_session.add(WellnessRow(athlete_id=1, date=date(2026, 2, 1), hrv=58.0))
        await db_session.flush()
        db_session.add(WellnessRow(athlete_id=1, date=date(2026, 2, 1), hrv=60.0))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
