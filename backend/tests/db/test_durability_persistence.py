"""DB-backed integration tests for session-durability persistence (RID-10, §6).

Proves that :func:`app.services.durability.recompute_durability`:

- derives the engine inputs (intensity + HR samples) from the stored
  activity streams and persists one ``session_durability`` row per
  activity, with EF halves, decoupling and the reference-band verdict
  (§7.6), round-tripping per activity;
- stamps ``engine_version`` on every persisted row (§6);
- is idempotent: recomputing the window upserts the existing rows;
- represents a ``not_steady`` session as a row with the explicit status,
  NULL decoupling/EF fields and the measured drift — never a fabricated
  value;
- reports activities it cannot assess (unsupported sport, missing stream,
  engine-rejected input) as skipped with a reason, never silently.

Requires the compose Postgres; skips cleanly without it (see conftest).
"""

import datetime as dt
from collections.abc import Sequence
from typing import Any, cast

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import ActivityRow, SessionDurabilityRow
from app.services.durability import recompute_durability

pytestmark = pytest.mark.anyio

ATHLETE_ID = 1
ENGINE_VERSION = "test-0.0.0"
WINDOW_END = dt.date(2026, 8, 31)
COMPUTED_AT = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


async def _store_ride(
    db_session: AsyncSession,
    *,
    source_id: str,
    day: dt.date,
    power: Sequence[float | None],
    hr: Sequence[float | None],
) -> ActivityRow:
    """One bike ride with power + hr streams (1 Hz, aligned)."""
    start = dt.datetime.combine(day, dt.time(8, 0), tzinfo=dt.UTC)
    activity = await repository.upsert_activity(
        db_session,
        source_id=source_id,
        type="Ride",
        name=source_id,
        start_time=start,
        start_time_local=start.isoformat(),
        duration_s=len(power),
    )
    for stream_type, payload in (("power", power), ("hr", hr)):
        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type=stream_type,
            data=cast(list[float], payload),
        )
    return activity


STEADY_POWER = [200.0] * 100
# First half HR 150, second half 155 -> the §12.5 hand-calculated shape:
# EF 200/150 vs 200/155, decoupling (155-150)/155.
STEADY_HR = [150.0] * 50 + [155.0] * 50
EXPECTED_DECOUPLING = (200.0 / 150.0 - 200.0 / 155.0) / (200.0 / 150.0)

NOT_STEADY_POWER = [100.0] * 50 + [300.0] * 50
NOT_STEADY_HR = [150.0] * 100


async def _recompute(
    db_session: AsyncSession, *, engine_version: str = ENGINE_VERSION
) -> Any:
    return await recompute_durability(
        db_session,
        athlete_id=ATHLETE_ID,
        window_end=WINDOW_END,
        days=7,
        engine_version=engine_version,
    )


class TestSessionDurabilityRoundTrip:
    async def test_ok_session_round_trip(self, db_session: AsyncSession) -> None:
        await _store_ride(
            db_session,
            source_id="steady",
            day=WINDOW_END,
            power=STEADY_POWER,
            hr=STEADY_HR,
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.activities_considered == 1
        assert report.rows_upserted == 1
        assert report.not_steady_rows == 0
        assert report.skipped == ()
        row = (
            (await db_session.execute(_fresh(select(SessionDurabilityRow))))
            .scalar_one()
        )
        assert row.status == "ok"
        assert row.sport == "bike"
        assert row.ef_first_half == pytest.approx(200.0 / 150.0)
        assert row.ef_second_half == pytest.approx(200.0 / 155.0)
        assert row.decoupling == pytest.approx(EXPECTED_DECOUPLING)
        assert row.decoupling_pct == pytest.approx(EXPECTED_DECOUPLING * 100.0)
        assert row.within_reference_band is True
        assert row.reference_band == pytest.approx(0.05)
        assert row.n_samples == 100
        assert row.n_first_half == 50
        assert row.n_second_half == 50
        assert row.engine_version == ENGINE_VERSION

    async def test_not_steady_session_is_a_row_without_values(
        self, db_session: AsyncSession
    ) -> None:
        """The steadiness guard rejects the session: the persisted row
        carries ``status == "not_steady"``, NULL EF/decoupling and the
        measured drift — the evidence, never a fabricated decoupling."""
        await _store_ride(
            db_session,
            source_id="intervals",
            day=WINDOW_END,
            power=NOT_STEADY_POWER,
            hr=NOT_STEADY_HR,
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.rows_upserted == 1
        assert report.not_steady_rows == 1
        row = (
            (await db_session.execute(_fresh(select(SessionDurabilityRow))))
            .scalar_one()
        )
        assert row.status == "not_steady"
        assert row.ef_first_half is None
        assert row.ef_second_half is None
        assert row.decoupling is None
        assert row.decoupling_pct is None
        assert row.within_reference_band is None
        assert row.intensity_drift == pytest.approx(2.0)
        assert row.engine_version == ENGINE_VERSION


class TestSessionDurabilityIdempotency:
    async def test_recompute_upserts_no_duplicates_and_restamps_version(
        self, db_session: AsyncSession
    ) -> None:
        await _store_ride(
            db_session,
            source_id="steady",
            day=WINDOW_END,
            power=STEADY_POWER,
            hr=STEADY_HR,
        )
        await _store_ride(
            db_session,
            source_id="intervals",
            day=WINDOW_END - dt.timedelta(days=1),
            power=NOT_STEADY_POWER,
            hr=NOT_STEADY_HR,
        )
        await db_session.commit()

        await _recompute(db_session)
        await db_session.commit()
        assert await _count(db_session, SessionDurabilityRow) == 2

        await _recompute(db_session)
        await db_session.commit()
        assert await _count(db_session, SessionDurabilityRow) == 2

        await _recompute(db_session, engine_version="test-9.9")
        await db_session.commit()
        rows = (
            (await db_session.execute(_fresh(select(SessionDurabilityRow))))
            .scalars()
            .all()
        )
        assert {r.engine_version for r in rows} == {"test-9.9"}


class TestDurabilitySkippedContract:
    async def test_activity_without_hr_stream_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        """A ride with a power stream but no HR stream cannot yield an EF:
        reported as skipped with a reason, never persisted with a guess."""
        start = dt.datetime.combine(WINDOW_END, dt.time(8, 0), tzinfo=dt.UTC)
        activity = await repository.upsert_activity(
            db_session,
            source_id="nohr",
            type="Ride",
            name="nohr",
            start_time=start,
            start_time_local=start.isoformat(),
            duration_s=100,
        )
        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="power",
            data=list(STEADY_POWER),
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.rows_upserted == 0
        assert len(report.skipped) == 1
        assert "hr" in report.skipped[0].reason

    async def test_unsupported_sport_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        start = dt.datetime.combine(WINDOW_END, dt.time(8, 0), tzinfo=dt.UTC)
        await repository.upsert_activity(
            db_session,
            source_id="walk",
            type="Walk",
            name="walk",
            start_time=start,
            start_time_local=start.isoformat(),
            duration_s=3600,
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.activities_considered == 1
        assert report.rows_upserted == 0
        assert len(report.skipped) == 1
        assert "sport" in report.skipped[0].reason
