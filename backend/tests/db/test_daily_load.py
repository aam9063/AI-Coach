"""DB-backed integration tests for ``daily_load`` persistence (LOAD-10, first half).

Proves (a) a stored activity with a known HR stream produces exactly the
load the pure engine computes for the same inputs, (b) re-running the
persistence is idempotent (same rows updated, no duplicates), (c)
``engine_version`` is stamped on every row, and (d) activities with no
usable data are counted as skipped with a reason, never silently dropped.

Requires the compose Postgres; skips cleanly without it (see conftest).
"""

from datetime import UTC, date, datetime
from statistics import fmean
from typing import Any, cast

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import ActivityRow, DailyLoadRow
from app.engine.load import (
    ActivityLoadInput,
    ThresholdBundle,
    TrimpCoefficients,
    select_load_method,
)
from app.engine.pmc import compute_pmc_per_sport
from app.services.daily_load import COMBINED_SPORT_KEY, DailyLoadReport, recompute_daily_load

pytestmark = pytest.mark.anyio

ATHLETE_ID = 1
HR_REST, HR_MAX, LTHR = 65.0, 186.0, 169.0
COEFFS = TrimpCoefficients.from_sex("male")
THRESHOLDS = ThresholdBundle(lthr_bpm=LTHR, hr_max_bpm=HR_MAX, hr_rest_bpm=HR_REST)
ENGINE_VERSION = "test-0.0.0"

DAY = date(2026, 7, 1)
START = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


async def _store_activity(
    db_session: AsyncSession,
    *,
    source_id: str,
    type_: str = "Ride",
    hr_payload: list[float | None] | None = None,
    start: datetime = START,
    duration_s: int | None = 3600,
) -> ActivityRow:
    """Store one activity (default 1 h) and (optionally) its ``hr`` stream."""
    activity = await repository.upsert_activity(
        db_session,
        source_id=source_id,
        type=type_,
        name=type_,
        start_time=start,
        duration_s=duration_s,
    )
    if hr_payload is not None:
        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="hr",
            # The repository narrows the JSONB payload to list[float]; keep
            # the documented cast convention (see app/ingest/sync.py).
            data=cast(list[float], hr_payload),
        )
    return activity


async def _recompute(
    db_session: AsyncSession, *, days: int = 1
) -> DailyLoadReport:
    return await recompute_daily_load(
        db_session,
        athlete_id=ATHLETE_ID,
        window_end=DAY,
        days=days,
        thresholds=THRESHOLDS,
        coefficients=COEFFS,
        engine_version=ENGINE_VERSION,
    )


def _engine_hr_tss(hr_payload: list[float | None], duration_s: float) -> float:
    """The same computation through the pure engine, for parity assertions."""
    hr_avg = fmean(s for s in hr_payload if s is not None)
    selection = select_load_method(
        ActivityLoadInput(sport="Ride", duration_s=duration_s, hr_avg_bpm=hr_avg),
        THRESHOLDS,
        coefficients=COEFFS,
    )
    return selection.tss


class TestEngineParity:
    async def test_hr_stream_load_matches_engine_exactly(self, db_session):  # type: ignore[no-untyped-def]
        hr_payload = [150.0, None, 150.0, 160.0]  # gaps excluded -> mean 152.5
        await _store_activity(db_session, source_id="i-parity-1", hr_payload=hr_payload)

        await _recompute(db_session)
        await db_session.commit()

        expected_tss = _engine_hr_tss(hr_payload, 3600.0)
        rows = (
            (await db_session.execute(_fresh(select(DailyLoadRow)))).scalars().all()
        )
        ride = next(r for r in rows if r.sport == "ride")
        combined = next(r for r in rows if r.sport == COMBINED_SPORT_KEY)

        assert ride.tss == pytest.approx(expected_tss, rel=1e-12)
        assert combined.tss == pytest.approx(expected_tss, rel=1e-12)

        # PMC parity: the same daily series through the pure engine.
        expected_pmc = compute_pmc_per_sport({"ride": {DAY: expected_tss}})
        assert ride.ctl == pytest.approx(
            expected_pmc.per_sport["ride"].days[0].ctl, rel=1e-12
        )
        assert ride.atl == pytest.approx(
            expected_pmc.per_sport["ride"].days[0].atl, rel=1e-12
        )
        assert ride.tsb == pytest.approx(
            expected_pmc.per_sport["ride"].days[0].tsb, rel=1e-12
        )
        assert combined.ctl == pytest.approx(expected_pmc.combined.days[0].ctl, rel=1e-12)
        assert combined.atl == pytest.approx(expected_pmc.combined.days[0].atl, rel=1e-12)
        assert combined.tsb == pytest.approx(expected_pmc.combined.days[0].tsb, rel=1e-12)

    async def test_two_activities_same_day_are_summed_per_sport(self, db_session):  # type: ignore[no-untyped-def]
        first: list[float | None] = [150.0]
        second: list[float | None] = [170.0]
        await _store_activity(db_session, source_id="i-sum-1", hr_payload=first)
        await _store_activity(
            db_session,
            source_id="i-sum-2",
            hr_payload=second,
            start=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
        )

        await _recompute(db_session)
        await db_session.commit()

        expected = _engine_hr_tss(first, 3600.0) + _engine_hr_tss(second, 3600.0)
        ride = (
            await db_session.execute(
                _fresh(
                    select(DailyLoadRow).where(
                        DailyLoadRow.sport == "ride", DailyLoadRow.date == DAY
                    )
                )
            )
        ).scalar_one()
        assert ride.tss == pytest.approx(expected, rel=1e-12)


class TestIdempotency:
    async def test_rerunning_updates_rows_without_duplicates(self, db_session):  # type: ignore[no-untyped-def]
        await _store_activity(db_session, source_id="i-idem-1", hr_payload=[150.0])

        await _recompute(db_session)
        await db_session.commit()
        ids_first = [
            row.id
            for row in (
                await db_session.execute(_fresh(select(DailyLoadRow).order_by(DailyLoadRow.id)))
            )
            .scalars()
            .all()
        ]

        report_second = await _recompute(db_session)
        await db_session.commit()
        rows_second = (
            (await db_session.execute(_fresh(select(DailyLoadRow).order_by(DailyLoadRow.id))))
            .scalars()
            .all()
        )

        assert [row.id for row in rows_second] == ids_first  # same rows updated
        assert await _count(db_session, DailyLoadRow) == len(ids_first)
        assert report_second.rows_upserted == len(ids_first)


class TestEngineVersionStamp:
    async def test_every_row_carries_engine_version(self, db_session):  # type: ignore[no-untyped-def]
        await _store_activity(db_session, source_id="i-ver-1", hr_payload=[150.0])

        await _recompute(db_session)
        await db_session.commit()

        rows = (
            (await db_session.execute(_fresh(select(DailyLoadRow)))).scalars().all()
        )
        assert rows, "expected persisted daily_load rows"
        assert {row.engine_version for row in rows} == {ENGINE_VERSION}


class TestSkippedReporting:
    async def test_no_usable_data_is_skipped_with_reason(self, db_session):  # type: ignore[no-untyped-def]
        # Ride with no HR stream and no RPE: no applicable load method.
        await _store_activity(db_session, source_id="i-skip-1", hr_payload=None)

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.activities_considered == 1
        assert len(report.skipped) == 1
        skip = report.skipped[0]
        assert skip.activity_id is not None
        assert "no applicable load method" in skip.reason
        assert await _count(db_session, DailyLoadRow) == 0

    async def test_unknown_sport_is_skipped_with_reason(self, db_session):  # type: ignore[no-untyped-def]
        await _store_activity(
            db_session, source_id="i-skip-2", type_="Yoga", hr_payload=[120.0]
        )

        report = await _recompute(db_session)
        await db_session.commit()

        assert len(report.skipped) == 1
        assert "unknown sport" in report.skipped[0].reason
        assert await _count(db_session, DailyLoadRow) == 0

    async def test_window_without_activities_persists_nothing(self, db_session):  # type: ignore[no-untyped-def]
        report = await _recompute(db_session)

        assert report.activities_considered == 0
        assert report.rows_upserted == 0
        assert await _count(db_session, DailyLoadRow) == 0
