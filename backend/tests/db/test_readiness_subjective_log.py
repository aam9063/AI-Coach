"""WA-6 (write half) RED tests: the subjective report reaches the
readiness assessment through the service (§7.4).

:func:`app.services.readiness.recompute_readiness` previously passed
``subjective_fatigue_reported=None`` unconditionally (the module
docstring said "not stored yet"). These tests pin the new wiring:

- a ``subjective_log`` row for the as-of date with a reported fatigue
  level reaches the engine's ``readiness_assessment`` as the
  ``subjective_fatigue_reported`` context signal (bool semantics — the
  engine's, not a new one: ANY reported level counts, §7.4);
- the flagship difference: TSB very negative (one adverse signal) +
  reported fatigue → TWO agreeing adverse signals and the
  ``suggest_reduce_intensity`` warning fires, where before it did not;
- a day WITHOUT a subjective log row keeps ``None`` (not reported); a
  row with fatigue NULL but soreness/RPE present is NOT the fatigue
  signal (``False``, not ``True``).

Requires the compose Postgres; skips cleanly without it (conftest).
"""

import datetime as dt
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import ReadinessSnapshotRow
from app.services.readiness import recompute_readiness

pytestmark = pytest.mark.anyio

ATHLETE_ID = 1
ENGINE_VERSION = "test-0.0.0"
WINDOW_END = date(2026, 8, 31)
# hrv_readiness needs window (7) + baseline (60) days of series per as-of day.
HRV_SERIES_DAYS = 67
COMPUTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
TSB_VERY_NEGATIVE = -15.0


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    return select_stmt.execution_options(populate_existing=True)


def _series_days(window_end: date) -> list[date]:
    """Every date the recompute must read: the window plus the HRV history
    (7 rolling + 60 baseline days) ahead of it."""
    series_start = window_end - dt.timedelta(days=HRV_SERIES_DAYS - 1)
    n = (window_end - series_start).days + 1
    return [series_start + dt.timedelta(days=i) for i in range(n)]


async def _seed_wellness(db_session: AsyncSession, window_end: date) -> None:
    """Constant wellness values: every readiness signal stays inside its
    band (constant baseline → observed == mean → not flagged), so the
    ONLY adverse signals in this scenario are TSB and the report."""
    for day in _series_days(window_end):
        await repository.upsert_wellness(
            db_session,
            athlete_id=ATHLETE_ID,
            date=day,
            hrv=65.0,
            ln_hrv=4.174787099053614,
            resting_hr=50.0,
            sleep_minutes=480,
        )


async def _seed_tsb(db_session: AsyncSession, *, days: int) -> None:
    for offset in range(days):
        await repository.upsert_daily_load(
            db_session,
            athlete_id=ATHLETE_ID,
            date=WINDOW_END - dt.timedelta(days=offset),
            sport="combined",
            tss=0.0,
            ctl=40.0,
            atl=55.0,
            tsb=TSB_VERY_NEGATIVE,
            engine_version=ENGINE_VERSION,
            computed_at=COMPUTED_AT,
        )


async def _snapshots(db_session: AsyncSession) -> list[ReadinessSnapshotRow]:
    rows = (
        await db_session.execute(
            _fresh(
                select(ReadinessSnapshotRow)
                .where(ReadinessSnapshotRow.athlete_id == ATHLETE_ID)
                .order_by(ReadinessSnapshotRow.date)
            )
        )
    ).scalars().all()
    await db_session.commit()
    return list(rows)


async def _recompute(db_session: AsyncSession, *, days: int) -> None:
    await recompute_readiness(
        db_session,
        athlete_id=ATHLETE_ID,
        window_end=WINDOW_END,
        days=days,
        engine_version=ENGINE_VERSION,
    )
    await db_session.commit()


class TestSubjectiveFatigueReachesTheAssessment:
    async def test_reported_fatigue_turns_one_adverse_signal_into_two(
        self, db_session: AsyncSession
    ) -> None:
        """The flagship difference: before the report, TSB-very-negative
        alone is ONE adverse signal and no suggestion fires; after the
        log, fatigue agrees and the warning rule fires (§7.4)."""
        await _seed_wellness(db_session, WINDOW_END)
        await _seed_tsb(db_session, days=1)
        await _recompute(db_session, days=1)

        (before,) = await _snapshots(db_session)
        assert before.agreement_count == 1
        assert before.suggest_reduce_intensity is False
        assert list(before.adverse_signal_keys) == ["tsb_very_negative"]
        assert before.subjective_fatigue_reported is None

        await repository.upsert_subjective_log(
            db_session,
            athlete_id=ATHLETE_ID,
            date=WINDOW_END,
            rpe=None,
            fatigue=8,
            soreness=None,
            notes="muy cansado",
        )
        await db_session.commit()
        await _recompute(db_session, days=1)

        (after,) = await _snapshots(db_session)
        # Idempotent: still ONE snapshot row for the day, recomputed.
        assert after.agreement_count == 2
        assert after.suggest_reduce_intensity is True
        assert "subjective_fatigue" in after.adverse_signal_keys
        assert after.subjective_fatigue_reported is True
        assert any("fatigue" in reason.lower() for reason in after.reasons)

    async def test_log_on_one_day_does_not_leak_to_other_days(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_wellness(db_session, WINDOW_END)
        await _seed_tsb(db_session, days=2)
        report_day = WINDOW_END - dt.timedelta(days=1)  # the earlier day
        await repository.upsert_subjective_log(
            db_session,
            athlete_id=ATHLETE_ID,
            date=report_day,
            rpe=None,
            fatigue=7,
            soreness=None,
            notes=None,
        )
        await db_session.commit()

        await _recompute(db_session, days=2)

        by_date = {row.date: row for row in await _snapshots(db_session)}
        assert by_date[report_day].subjective_fatigue_reported is True
        assert by_date[report_day].agreement_count == 2
        assert by_date[WINDOW_END].subjective_fatigue_reported is None
        assert by_date[WINDOW_END].agreement_count == 1

    async def test_row_without_fatigue_is_not_the_fatigue_signal(
        self, db_session: AsyncSession
    ) -> None:
        """A log row carrying only RPE/soreness does NOT create the
        subjective-fatigue adverse signal: the engine consumes a BOOL
        ("fatigue reported"), not the level (§7.4; its semantics, not a
        new one)."""
        await _seed_wellness(db_session, WINDOW_END)
        await _seed_tsb(db_session, days=1)
        await repository.upsert_subjective_log(
            db_session,
            athlete_id=ATHLETE_ID,
            date=WINDOW_END,
            rpe=6.0,
            fatigue=None,
            soreness=3,
            notes=None,
        )
        await db_session.commit()

        await _recompute(db_session, days=1)

        (snapshot,) = await _snapshots(db_session)
        assert snapshot.subjective_fatigue_reported is False
        assert "subjective_fatigue" not in snapshot.adverse_signal_keys
        assert snapshot.agreement_count == 1
