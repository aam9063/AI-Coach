"""RPE ingestion tests (ODD LOAD-12): owner-entered RPE is INPUT data.

Intervals.icu stores an athlete-entered RPE per activity (``icu_rpe``,
integer scale 1-10, editable on the activity page) with the payload
aliases ``session_rpe`` and ``perceived_exertion``. The ``feel`` field
(1-5) is returned INVERTED by the API and is deliberately ignored.

Contract under test:
- aliases are accepted in the documented priority order (icu_rpe first);
- values outside 1-10 are REJECTED with a clear, reportable reason and
  never stored silently (rpe stays None and the reason is reported);
- a missing or null RPE stays None (no reason, no invention);
- the stored value round-trips through the ``activity.rpe`` column via
  the idempotent sync upsert.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import repository
from app.db.models import ActivityRow
from app.ingest.models import Activity, Stream, Wellness
from app.ingest.sync import SyncResult, extract_activity_rpe, sync_date_range

pytestmark = pytest.mark.anyio

OLDEST = "2026-01-01"
NEWEST = "2026-02-01"


def _activity(aid: str, **extras: Any) -> Activity:
    payload: dict[str, Any] = {
        "id": aid,
        "name": f"Session {aid}",
        "type": "WeightTraining",
        "start_date": "2026-01-15T08:30:00+00:00",
        "start_date_local": "2026-01-15T09:30:00+01:00",
        "moving_time": 3600,
    }
    payload.update(extras)
    return Activity.model_validate(payload)


class TestExtractActivityRpe:
    def test_icu_rpe_is_accepted(self) -> None:
        value, reason = extract_activity_rpe(_activity("a1", icu_rpe=7))
        assert value == 7
        assert reason is None

    def test_session_rpe_alias_is_accepted(self) -> None:
        value, reason = extract_activity_rpe(_activity("a1", session_rpe=8))
        assert value == 8
        assert reason is None

    def test_perceived_exertion_alias_is_accepted(self) -> None:
        value, reason = extract_activity_rpe(_activity("a1", perceived_exertion=6))
        assert value == 6
        assert reason is None

    def test_icu_rpe_wins_over_the_aliases(self) -> None:
        value, reason = extract_activity_rpe(
            _activity("a1", icu_rpe=7, session_rpe=3, perceived_exertion=2)
        )
        assert value == 7
        assert reason is None

    def test_absent_rpe_is_none_without_reason(self) -> None:
        value, reason = extract_activity_rpe(_activity("a1"))
        assert value is None
        assert reason is None

    def test_null_rpe_stays_none(self) -> None:
        value, reason = extract_activity_rpe(_activity("a1", icu_rpe=None))
        assert value is None
        assert reason is None

    @pytest.mark.parametrize("out_of_range", [0, 11, -3, 99])
    def test_out_of_range_value_is_rejected_with_reason(self, out_of_range: int) -> None:
        value, reason = extract_activity_rpe(_activity("a1", icu_rpe=out_of_range))
        assert value is None
        assert reason is not None
        assert str(out_of_range) in reason
        assert "1" in reason and "10" in reason  # the valid scale is named

    def test_boundary_values_are_accepted(self) -> None:
        for rpe in (1, 10):
            value, reason = extract_activity_rpe(_activity("a1", icu_rpe=rpe))
            assert value == rpe
            assert reason is None

    def test_non_numeric_value_is_rejected_with_reason(self) -> None:
        value, reason = extract_activity_rpe(_activity("a1", icu_rpe="hard"))
        assert value is None
        assert reason is not None
        assert "hard" in reason

    def test_feel_is_ignored(self) -> None:
        # The API returns ``feel`` (1-5) inverted; it must never be read
        # as an RPE.
        value, reason = extract_activity_rpe(_activity("a1", feel=4))
        assert value is None
        assert reason is None


def _session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


class FakeClient:
    """Minimal duck-typed IntervalsClient (no streams, no wellness)."""

    def __init__(self, activities: list[Activity]) -> None:
        self._activities = activities

    def list_activities(self, oldest: str, newest: str) -> list[Activity]:
        return list(self._activities)

    def get_streams(self, activity_id: str) -> list[Stream]:
        return []

    def get_wellness(self, oldest: str, newest: str) -> list[Wellness]:
        return []

    def download_fit_file(self, activity_id: str) -> bytes:
        return b""


class TestSyncPersistsRpe:
    async def test_icu_rpe_is_persisted_on_the_activity_row(
        self, db_engine: AsyncEngine
    ) -> None:
        client = FakeClient([_activity("i-rpe-1", icu_rpe=7)])
        result = await sync_date_range(
            OLDEST, NEWEST, _session_factory(db_engine), client
        )
        assert result.activities_synced == 1
        async with _session_factory(db_engine)() as session:
            row = (
                await session.execute(_fresh(select(ActivityRow)))
            ).scalar_one()
            assert row.rpe == 7

    async def test_alias_value_is_persisted(self, db_engine: AsyncEngine) -> None:
        client = FakeClient([_activity("i-rpe-2", session_rpe=5)])
        await sync_date_range(OLDEST, NEWEST, _session_factory(db_engine), client)
        async with _session_factory(db_engine)() as session:
            row = (
                await session.execute(_fresh(select(ActivityRow)))
            ).scalar_one()
            assert row.rpe == 5

    async def test_out_of_range_rpe_is_rejected_not_stored(
        self, db_engine: AsyncEngine
    ) -> None:
        client = FakeClient([_activity("i-rpe-3", icu_rpe=42)])
        result = await sync_date_range(
            OLDEST, NEWEST, _session_factory(db_engine), client
        )
        # The activity itself still syncs (the RPE is one field, not the item).
        assert result.activities_synced == 1
        async with _session_factory(db_engine)() as session:
            row = (
                await session.execute(_fresh(select(ActivityRow)))
            ).scalar_one()
            assert row.rpe is None
        # ... and the rejection is reported, never silent.
        assert result.rpe_rejected == 1
        assert any("42" in reason for reason in result.rpe_rejection_reasons)

    async def test_no_rpe_stores_none_and_reports_nothing(
        self, db_engine: AsyncEngine
    ) -> None:
        client = FakeClient([_activity("i-rpe-4")])
        result = await sync_date_range(
            OLDEST, NEWEST, _session_factory(db_engine), client
        )
        assert result.activities_synced == 1
        async with _session_factory(db_engine)() as session:
            row = (
                await session.execute(_fresh(select(ActivityRow)))
            ).scalar_one()
            assert row.rpe is None
        assert result.rpe_rejected == 0
        assert result.rpe_rejection_reasons == []

    async def test_resync_updates_the_stored_rpe(self, db_engine: AsyncEngine) -> None:
        """Idempotency: a re-sync with a corrected RPE overwrites the old one
        (the same (source, source_id) anchor), like every other column."""
        factory = _session_factory(db_engine)
        await sync_date_range(
            OLDEST, NEWEST, factory, FakeClient([_activity("i-rpe-5", icu_rpe=3)])
        )
        await sync_date_range(
            OLDEST, NEWEST, factory, FakeClient([_activity("i-rpe-5", icu_rpe=8)])
        )
        async with factory() as session:
            rows = (
                await session.execute(_fresh(select(ActivityRow)))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].rpe == 8


class TestRepositoryRpeRoundTrip:
    async def test_upsert_activity_persists_rpe(self, db_session: AsyncSession) -> None:
        created = await repository.upsert_activity(
            db_session,
            source_id="i-rpe-rt",
            type="WeightTraining",
            name="Gym",
            start_time=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
            duration_s=3600,
            rpe=7,
        )
        await db_session.commit()
        reloaded = (
            await db_session.execute(
                _fresh(select(ActivityRow).where(ActivityRow.id == created.id))
            )
        ).scalar_one()
        assert reloaded.rpe == 7

    async def test_rpe_defaults_to_none(self, db_session: AsyncSession) -> None:
        row = await repository.upsert_activity(
            db_session,
            source_id="i-rpe-rt-none",
            type="Ride",
            name="Ride",
            start_time=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        )
        await db_session.commit()
        reloaded = (
            await db_session.execute(
                _fresh(select(ActivityRow).where(ActivityRow.id == row.id))
            )
        ).scalar_one()
        assert reloaded.rpe is None


def test_sync_result_declares_rpe_rejection_fields() -> None:
    """The reportable-reason contract lives on SyncResult."""
    fields = SyncResult.__dataclass_fields__
    assert "rpe_rejected" in fields
    assert "rpe_rejection_reasons" in fields
