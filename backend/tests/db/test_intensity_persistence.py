"""DB-backed integration tests for weekly-intensity persistence (RID-10, §6).

Proves that :func:`app.services.intensity.recompute_intensity`:

- derives per-session source-zone seconds from the stored activity streams
  (time-weighted via the ``time`` stream — real Intervals streams are NOT
  1 Hz) and persists one row per (athlete, ISO week, sport);
- enforces the engine's CANONICAL sport->modality map (a different map is
  rejected loudly, because the engine's weekly aggregation validates
  against it);
- stamps ``engine_version`` on every persisted row (§6);
- is idempotent: recomputing the window upserts the existing rows;
- represents an engine ``no_data`` sport-week as a row with
  ``status == "no_data"`` and NULL percentages — never a fabricated 0%
  split;
- reports activities it cannot classify (unsupported sport, missing
  threshold or missing stream) as skipped with a reason, never silently.

Requires the compose Postgres; skips cleanly without it (see conftest).
"""

import datetime as dt
from typing import Any, cast

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import ActivityRow, WeeklyIntensityRow
from app.engine.intensity import (
    DEFAULT_FIRST_THRESHOLD_PCTS,
    DEFAULT_SECOND_THRESHOLD_PCTS,
)
from app.services.intensity import recompute_intensity

pytestmark = pytest.mark.anyio

ATHLETE_ID = 1
ENGINE_VERSION = "test-0.0.0"
FTP = 200.0
COMPUTED_AT = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
# ISO week containing Mon 2026-08-03 .. Sun 2026-08-09, and the next one.
WEEK1_MONDAY = dt.date(2026, 8, 3)
WEEK2_MONDAY = dt.date(2026, 8, 10)
# Canonical bike modality (bike_power, Coggan table, LT1 76 % / LT2 106 %
# FTP): 100 W = 50 % FTP -> Z1; 300 W = 150 % FTP -> Z6 -> Z3.
Z1_WATTS = 100.0
Z3_WATTS = 300.0


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


async def _store_power_ride(
    db_session: AsyncSession,
    *,
    source_id: str,
    day: dt.date,
    watts: float,
    seconds: int = 60,
) -> ActivityRow:
    """One 60 s ride whose power stream holds constant watts, 1 Hz axis."""
    start = dt.datetime.combine(day, dt.time(8, 0), tzinfo=dt.UTC)
    activity = await repository.upsert_activity(
        db_session,
        source_id=source_id,
        type="Ride",
        name=source_id,
        start_time=start,
        start_time_local=start.isoformat(),
        duration_s=seconds,
    )
    await repository.upsert_activity_stream(
        db_session,
        activity_id=activity.id,
        stream_type="power",
        data=cast(list[float], [watts] * seconds),
    )
    await repository.upsert_activity_stream(
        db_session,
        activity_id=activity.id,
        stream_type="time",
        data=cast(list[float], [float(i) for i in range(seconds)]),
    )
    return activity


async def _recompute(
    db_session: AsyncSession, *, engine_version: str = ENGINE_VERSION
) -> Any:
    return await recompute_intensity(
        db_session,
        athlete_id=ATHLETE_ID,
        window_end=dt.date(2026, 8, 16),
        days=14,
        engine_version=engine_version,
        sport_modality={"run": "run_hr", "bike": "bike_power", "swim": "swim_pace"},
        first_threshold_pcts=dict(DEFAULT_FIRST_THRESHOLD_PCTS),
        second_threshold_pcts=dict(DEFAULT_SECOND_THRESHOLD_PCTS),
        ftp_watts=FTP,
    )


class TestWeeklyIntensityRoundTrip:
    async def test_per_sport_round_trip(self, db_session: AsyncSession) -> None:
        # Two Z1 rides in ISO week 1, one Z3 ride in ISO week 2.
        await _store_power_ride(
            db_session, source_id="w1a", day=WEEK1_MONDAY, watts=Z1_WATTS
        )
        await _store_power_ride(
            db_session, source_id="w1b", day=WEEK1_MONDAY + dt.timedelta(days=2),
            watts=Z1_WATTS,
        )
        await _store_power_ride(
            db_session, source_id="w2a", day=WEEK2_MONDAY, watts=Z3_WATTS
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 3
        assert report.skipped == ()
        rows = (
            (await db_session.execute(_fresh(select(WeeklyIntensityRow))))
            .scalars()
            .all()
        )
        bike_w1 = next(
            r
            for r in rows
            if r.sport == "bike"
            and r.week_start == WEEK1_MONDAY
        )
        assert bike_w1.status == "data"
        assert bike_w1.total_seconds == pytest.approx(120.0)
        assert bike_w1.z1_seconds == pytest.approx(120.0)
        assert bike_w1.z3_seconds == pytest.approx(0.0)
        assert bike_w1.percentages is not None
        assert bike_w1.percentages == pytest.approx([100.0, 0.0, 0.0])

        bike_w2 = next(
            r
            for r in rows
            if r.sport == "bike" and r.week_start == WEEK2_MONDAY
        )
        assert bike_w2.status == "data"
        assert bike_w2.z3_seconds == pytest.approx(60.0)
        assert bike_w2.percentages == pytest.approx([0.0, 0.0, 100.0])

    async def test_no_data_sport_week_is_a_row_with_null_percentages(
        self, db_session: AsyncSession
    ) -> None:
        """A week with sessions of one sport but none of another persists
        the other sport's ``no_data`` week: a row with the explicit status
        and NULL percentages — never a fabricated 0% split."""
        await _store_power_ride(
            db_session, source_id="w1a", day=WEEK1_MONDAY, watts=Z1_WATTS
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 1
        assert report.no_data_rows >= 2
        run_w1 = (
            (
                await db_session.execute(
                    _fresh(
                        select(WeeklyIntensityRow).where(
                            WeeklyIntensityRow.sport == "run",
                            WeeklyIntensityRow.week_start == WEEK1_MONDAY,
                        )
                    )
                )
            )
            .scalar_one()
        )
        assert run_w1.status == "no_data"
        assert run_w1.total_seconds == 0.0
        assert run_w1.z1_seconds == 0.0
        assert run_w1.percentages is None  # never a fabricated split
        assert run_w1.engine_version == ENGINE_VERSION


class TestWeeklyIntensityIdempotency:
    async def test_recompute_upserts_no_duplicates_and_restamps_version(
        self, db_session: AsyncSession
    ) -> None:
        await _store_power_ride(
            db_session, source_id="w1a", day=WEEK1_MONDAY, watts=Z1_WATTS
        )
        await db_session.commit()

        await _recompute(db_session)
        await db_session.commit()
        first = await _count(db_session, WeeklyIntensityRow)
        await _recompute(db_session)
        await db_session.commit()
        assert await _count(db_session, WeeklyIntensityRow) == first

        await _recompute(db_session, engine_version="test-9.9")
        await db_session.commit()
        rows = (
            (await db_session.execute(_fresh(select(WeeklyIntensityRow))))
            .scalars()
            .all()
        )
        assert first > 0
        assert {r.engine_version for r in rows} == {"test-9.9"}


class TestCanonicalModalityGuard:
    async def test_non_canonical_modality_map_is_rejected_loudly(
        self, db_session: AsyncSession
    ) -> None:
        """The engine's weekly aggregation validates zone seconds against
        its canonical SPORT_MODALITY tables, so a caller map that deviates
        is rejected before any row is written — never silently
        re-interpreted."""
        import pytest as _pytest

        with _pytest.raises(ValueError, match="canonical"):
            await recompute_intensity(
                db_session,
                athlete_id=ATHLETE_ID,
                window_end=dt.date(2026, 8, 16),
                days=14,
                engine_version=ENGINE_VERSION,
                sport_modality={"run": "run_hr", "bike": "bike_hr", "swim": "swim_pace"},
                first_threshold_pcts={
                    "run_hr": 85.0,
                    "bike_hr": 90.0,
                    "swim_pace": 95.0,
                },
                second_threshold_pcts={
                    "run_hr": 100.0,
                    "bike_hr": 100.0,
                    "swim_pace": 100.0,
                },
                lthr_bpm=160.0,
            )


class TestIntensitySkippedContract:
    async def test_unsupported_sport_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        """A strength session is not one of the three intensity sports: it
        is reported as skipped, never silently dropped."""
        start = dt.datetime.combine(WEEK1_MONDAY, dt.time(8, 0), tzinfo=dt.UTC)
        await repository.upsert_activity(
            db_session,
            source_id="gym",
            type="WeightTraining",
            name="gym",
            start_time=start,
            start_time_local=start.isoformat(),
            duration_s=3600,
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.activities_considered == 1
        assert report.sessions_derived == 0
        assert len(report.skipped) == 1
        assert report.skipped[0].activity_id is not None
        assert "sport" in report.skipped[0].reason

    async def test_missing_stream_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        """A ride without the modality's stream (bike_power needs
        ``power``) is skipped with a reason — no empty fabricated
        session."""
        start = dt.datetime.combine(WEEK1_MONDAY, dt.time(8, 0), tzinfo=dt.UTC)
        await repository.upsert_activity(
            db_session,
            source_id="noride",
            type="Ride",
            name="noride",
            start_time=start,
            start_time_local=start.isoformat(),
            duration_s=3600,
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 0
        assert len(report.skipped) == 1
        assert "power" in report.skipped[0].reason


class TestPowerlessBikeFallback:
    """The owner has NO power meter: their rides carry ``hr``/``speed``/
    ``distance``/``altitude``/``time`` streams only. The service must
    select the bike_hr modality for such rides (the load engine's
    power-first preference) instead of skipping every ride."""

    LTHR = 200.0  # test anchor: 150 bpm = 75 % LTHR (Z1); 210 = 105 % (Z5a)

    async def _store_hr_ride(
        self,
        db_session: AsyncSession,
        *,
        source_id: str,
        day: dt.date,
        bpm: float,
        seconds: int = 60,
        with_power: bool = False,
        power_all_none: bool = False,
    ) -> ActivityRow:
        """One 60 s ride with an HR stream (and optionally a power stream
        that is real or entirely ``None`` gaps)."""
        start = dt.datetime.combine(day, dt.time(8, 0), tzinfo=dt.UTC)
        activity = await repository.upsert_activity(
            db_session,
            source_id=source_id,
            type="Ride",
            name=source_id,
            start_time=start,
            start_time_local=start.isoformat(),
            duration_s=seconds,
        )
        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="hr",
            data=cast(list[float], [bpm] * seconds),
        )
        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="time",
            data=cast(list[float], [float(i) for i in range(seconds)]),
        )
        if with_power or power_all_none:
            payload: list[float | None] = (
                [None] * seconds if power_all_none else [100.0] * seconds
            )
            await repository.upsert_activity_stream(
                db_session,
                activity_id=activity.id,
                stream_type="power",
                data=cast(list[float], payload),
            )
        return activity

    async def _recompute(
        self, db_session: AsyncSession
    ) -> Any:
        return await recompute_intensity(
            db_session,
            athlete_id=ATHLETE_ID,
            window_end=dt.date(2026, 8, 16),
            days=14,
            engine_version=ENGINE_VERSION,
            sport_modality={
                "run": "run_hr",
                "bike": "bike_power",
                "swim": "swim_pace",
            },
            first_threshold_pcts=dict(DEFAULT_FIRST_THRESHOLD_PCTS),
            second_threshold_pcts=dict(DEFAULT_SECOND_THRESHOLD_PCTS),
            ftp_watts=FTP,
            lthr_bpm=self.LTHR,
        )

    async def test_powerless_ride_with_hr_produces_bike_rows(
        self, db_session: AsyncSession
    ) -> None:
        """A ride with only HR + time streams classifies on the bike_hr
        table and lands in the bike sport's weekly rows (never skipped)."""
        await self._store_hr_ride(
            db_session,
            source_id="hr-ride",
            day=WEEK1_MONDAY,
            bpm=150.0,  # 75 % LTHR -> Friel bike Z1 -> 3-zone Z1
        )
        await db_session.commit()

        report = await self._recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 1
        assert report.skipped == ()
        bike_w1 = (
            (
                await db_session.execute(
                    _fresh(
                        select(WeeklyIntensityRow).where(
                            WeeklyIntensityRow.sport == "bike",
                            WeeklyIntensityRow.week_start == WEEK1_MONDAY,
                        )
                    )
                )
            ).scalar_one()
        )
        assert bike_w1.status == "data"
        assert bike_w1.total_seconds == pytest.approx(60.0)
        assert bike_w1.percentages == pytest.approx([100.0, 0.0, 0.0])

    async def test_all_none_power_stream_falls_back_to_hr(
        self, db_session: AsyncSession
    ) -> None:
        """The "usable" boundary: a power stream that EXISTS but is
        entirely ``None`` gaps is NOT usable — the ride still classifies
        on bike_hr."""
        await self._store_hr_ride(
            db_session,
            source_id="gap-power-ride",
            day=WEEK1_MONDAY,
            bpm=150.0,
            power_all_none=True,
        )
        await db_session.commit()

        report = await self._recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 1
        assert report.skipped == ()
        bike_w1 = (
            (
                await db_session.execute(
                    _fresh(
                        select(WeeklyIntensityRow).where(
                            WeeklyIntensityRow.sport == "bike",
                            WeeklyIntensityRow.week_start == WEEK1_MONDAY,
                        )
                    )
                )
            ).scalar_one()
        )
        assert bike_w1.status == "data"
        assert bike_w1.percentages == pytest.approx([100.0, 0.0, 0.0])

    async def test_powered_ride_still_uses_bike_power(
        self, db_session: AsyncSession
    ) -> None:
        """With usable power the canonical bike_power classification is
        unchanged (power wins over HR)."""
        await self._store_hr_ride(
            db_session,
            source_id="power-ride",
            day=WEEK1_MONDAY,
            bpm=150.0,
            with_power=True,
        )
        await db_session.commit()

        report = await self._recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 1
        assert report.skipped == ()
        bike_w1 = (
            (
                await db_session.execute(
                    _fresh(
                        select(WeeklyIntensityRow).where(
                            WeeklyIntensityRow.sport == "bike",
                            WeeklyIntensityRow.week_start == WEEK1_MONDAY,
                        )
                    )
                )
            ).scalar_one()
        )
        assert bike_w1.status == "data"
        # 100 W at FTP 200 = 50 % FTP -> Coggan Z1 -> 3-zone Z1 (the
        # bike_power table, not the HR one).
        assert bike_w1.percentages == pytest.approx([100.0, 0.0, 0.0])

    async def test_ride_without_power_or_hr_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        """No usable modality at all is still an explicit, named skip —
        never a silent drop and never an empty fabricated session."""
        start = dt.datetime.combine(WEEK1_MONDAY, dt.time(8, 0), tzinfo=dt.UTC)
        await repository.upsert_activity(
            db_session,
            source_id="bare-ride",
            type="Ride",
            name="bare-ride",
            start_time=start,
            start_time_local=start.isoformat(),
            duration_s=3600,
        )
        await db_session.commit()

        report = await self._recompute(db_session)
        await db_session.commit()

        assert report.sessions_derived == 0
        assert len(report.skipped) == 1
        reason = report.skipped[0].reason
        assert "power" in reason
        assert "bike_hr" in reason
