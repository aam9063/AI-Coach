"""ING-3 RED: upsert repository tests (create-then-update without duplicates).

Upserts must be idempotent: calling the same upsert twice with the same
source key updates the existing row rather than inserting a duplicate
(PROJECT_BRIEF.md §5.1 / §12.2 acceptance).
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import ActivityRow, ActivityStreamRow, WellnessRow

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 2, 1, 8, 30, tzinfo=UTC)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


class TestUpsertActivity:
    async def test_create_then_update_without_duplicate(self, db_session):  # type: ignore[no-untyped-def]
        created = await repository.upsert_activity(
            db_session,
            source_id="i163428838",
            type="Ride",
            name="Original",
            start_time=_T0,
            start_time_local="2026-02-01T09:30:00+01:00",
            distance_m=10000.0,
            duration_s=3600,
            elevation_m=120.0,
            intervals_icu_load=90.0,
        )
        assert created.id is not None
        assert created.source == "intervals"

        updated = await repository.upsert_activity(
            db_session,
            source_id="i163428838",
            type="Ride",
            name="Renamed",
            start_time=_T0,
            distance_m=10050.0,
            duration_s=3660,
        )

        assert updated.id == created.id  # same row updated, not re-inserted
        assert await _count(db_session, ActivityRow) == 1
        stored = (
            await db_session.execute(
                _fresh(
                    select(ActivityRow).where(ActivityRow.source_id == "i163428838")
                )
            )
        ).scalar_one()
        assert stored.name == "Renamed"
        assert stored.distance_m == 10050.0
        assert stored.duration_s == 3660
        assert stored.elevation_m is None  # absent on update -> cleared, keeps row truthful

    async def test_different_source_ids_create_distinct_rows(self, db_session):  # type: ignore[no-untyped-def]
        await repository.upsert_activity(
            db_session, source_id="i163428840", type="Run", name="A", start_time=_T0
        )
        await repository.upsert_activity(
            db_session, source_id="i163428841", type="Run", name="B", start_time=_T0
        )
        assert await _count(db_session, ActivityRow) == 2


class TestUpsertActivityStream:
    async def test_attach_then_replace_without_duplicate(self, db_session):  # type: ignore[no-untyped-def]
        activity = await repository.upsert_activity(
            db_session,
            source_id="i163419945",
            type="Ride",
            name="Ride",
            start_time=_T0,
        )

        first = await repository.upsert_activity_stream(
            db_session, activity_id=activity.id, stream_type="power", data=[0.0, 100.0]
        )
        second = await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="power",
            data=[0.0, 110.0, 130.0],
        )

        assert second.id == first.id
        assert await _count(db_session, ActivityStreamRow) == 1
        stored = (
            await db_session.execute(
                _fresh(
                    select(ActivityStreamRow).where(
                        ActivityStreamRow.activity_id == activity.id,
                        ActivityStreamRow.stream_type == "power",
                    )
                )
            )
        ).scalar_one()
        assert stored.payload == [0.0, 110.0, 130.0]


class TestUpsertWellness:
    async def test_same_date_updates_without_duplicate(self, db_session):  # type: ignore[no-untyped-def]
        first = await repository.upsert_wellness(
            db_session,
            athlete_id=1,
            date=date(2026, 2, 1),
            hrv=58.0,
            weight=70.5,
            intervals_icu_ctl=71.2,
            intervals_icu_atl=55.3,
        )
        second = await repository.upsert_wellness(
            db_session,
            athlete_id=1,
            date=date(2026, 2, 1),
            hrv=60.0,
            sleep_minutes=430,
        )

        assert second.id == first.id
        assert await _count(db_session, WellnessRow) == 1
        stored = (
            await db_session.execute(_fresh(select(WellnessRow)))
        ).scalar_one()
        assert stored.hrv == 60.0
        assert stored.sleep_minutes == 430
        assert stored.weight is None
        # Not re-supplied on the update: cleared, not stale.
        assert stored.intervals_icu_ctl is None
        assert stored.intervals_icu_atl is None

    async def test_cross_check_ctl_atl_round_trip(self, db_session):  # type: ignore[no-untyped-def]
        """The Intervals CTL/ATL cross-check columns (§5.1) persist and are
        NULL when not supplied — never a silent 0 (a real 0 CTL is data)."""
        row = await repository.upsert_wellness(
            db_session,
            athlete_id=1,
            date=date(2026, 2, 2),
            intervals_icu_ctl=0.0,
            intervals_icu_atl=42.5,
        )
        assert row.intervals_icu_ctl == 0.0
        assert row.intervals_icu_atl == 42.5
