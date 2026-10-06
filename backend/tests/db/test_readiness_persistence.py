"""DB-backed integration tests for readiness persistence (RID-10, §6).

Proves that :func:`app.services.readiness.recompute_readiness`:

- reads the readiness inputs from ``wellness`` (HRV ln(rMSSD), resting HR,
  sleep) and the TSB context from the ``daily_load`` ``combined`` rows and
  persists one readiness snapshot row per (athlete, date) — the structured
  signals round-trip as JSONB, never collapsed into a score (§7.4);
- stamps ``engine_version`` on every persisted row (§6);
- is idempotent: recomputing the window upserts the existing rows instead
  of duplicating them;
- represents an ``insufficient_data`` signal as a signal with
  ``status == "insufficient_data"`` and NULL observation inside the
  persisted JSONB — never a fabricated zero or default;
- reports days it cannot assess (no ``daily_load`` TSB row) as skipped with
  a reason, never silently.

Requires the compose Postgres; skips cleanly without it (see conftest).
"""

import datetime as dt
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import DailyLoadRow, ReadinessSnapshotRow
from app.services.readiness import recompute_readiness

pytestmark = pytest.mark.anyio

ATHLETE_ID = 1
ENGINE_VERSION = "test-0.0.0"
WINDOW_END = date(2026, 8, 31)
DAYS = 3
# hrv_readiness needs window (7) + baseline (60) days of series per as-of day.
HRV_SERIES_DAYS = 67
COMPUTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


def _series_days() -> list[date]:
    """Every date the recompute must read: the 3-day window plus the HRV
    history (7 rolling + 60 baseline days) ahead of the window start."""
    window_start = WINDOW_END - dt.timedelta(days=DAYS - 1)
    first = window_start - dt.timedelta(days=HRV_SERIES_DAYS - 1)
    n = (WINDOW_END - first).days + 1
    return [first + dt.timedelta(days=i) for i in range(n)]


async def _seed_wellness(
    db_session: AsyncSession,
    *,
    hrv_rmssd: float | None = 65.0,
    resting_hr: float | None = 50.0,
    sleep_minutes: int | None = 480,
) -> None:
    """Seed one wellness row per needed day (values or explicit missing)."""
    for day in _series_days():
        await repository.upsert_wellness(
            db_session,
            athlete_id=ATHLETE_ID,
            date=day,
            hrv=hrv_rmssd,
            ln_hrv=None if hrv_rmssd is None else 4.174787099053614,
            resting_hr=resting_hr,
            sleep_minutes=sleep_minutes,
        )


async def _seed_tsb(db_session: AsyncSession, tsb: float = 1.0) -> None:
    """Seed a daily_load ``combined`` row per window date (the TSB input)."""
    for offset in range(DAYS):
        await repository.upsert_daily_load(
            db_session,
            athlete_id=ATHLETE_ID,
            date=WINDOW_END - dt.timedelta(days=offset),
            sport="combined",
            tss=0.0,
            ctl=40.0,
            atl=30.0,
            tsb=tsb,
            engine_version=ENGINE_VERSION,
            computed_at=COMPUTED_AT,
        )


async def _recompute(
    db_session: AsyncSession, *, engine_version: str = ENGINE_VERSION
) -> Any:
    return await recompute_readiness(
        db_session,
        athlete_id=ATHLETE_ID,
        window_end=WINDOW_END,
        days=DAYS,
        engine_version=engine_version,
    )


class TestReadinessSnapshotRoundTrip:
    async def test_round_trip_persists_structured_signals(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_wellness(db_session)
        await _seed_tsb(db_session)
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.rows_upserted == DAYS
        assert report.skipped == ()
        rows = (
            (await db_session.execute(_fresh(select(ReadinessSnapshotRow))))
            .scalars()
            .all()
        )
        assert len(rows) == DAYS
        row = next(r for r in rows if r.date == WINDOW_END)
        assert row.athlete_id == ATHLETE_ID
        # The structured signals round-trip: one entry per signal key, in
        # the engine's stable order, with the assessed values — never a
        # composite score.
        assert [s["key"] for s in row.signals] == [
            "hrv_ln_rmssd",
            "resting_hr",
            "sleep_duration",
        ]
        assert all(s["status"] == "assessed" for s in row.signals)
        assert all(s["direction"] == "normal" for s in row.signals)
        assert all(s["observed"] is not None for s in row.signals)
        assert row.signals[0]["baseline_mean"] is not None
        assert row.agreement_count == 0
        assert row.suggest_reduce_intensity is False
        assert row.suggestion is None
        assert row.tsb == pytest.approx(1.0)
        assert row.adverse_signal_keys == []
        assert row.engine_version == ENGINE_VERSION
        assert row.computed_at is not None

    async def test_idempotent_recompute_and_engine_version_stamp(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_wellness(db_session)
        await _seed_tsb(db_session)
        await db_session.commit()

        await _recompute(db_session)
        await db_session.commit()
        await _recompute(db_session)
        await db_session.commit()
        assert await _count(db_session, ReadinessSnapshotRow) == DAYS

        # A second recompute with a NEW engine version re-stamps every row
        # (§6: every persisted engine output carries the engine version).
        await _recompute(db_session, engine_version="test-9.9")
        await db_session.commit()
        rows = (
            (await db_session.execute(_fresh(select(ReadinessSnapshotRow))))
            .scalars()
            .all()
        )
        assert len(rows) == DAYS
        assert {r.engine_version for r in rows} == {"test-9.9"}


class TestReadinessStatusRepresentation:
    async def test_insufficient_data_signal_persisted_not_fabricated(
        self, db_session: AsyncSession
    ) -> None:
        """Wellness rows exist but carry no measurement: the persisted
        signals are explicit ``insufficient_data`` with NULL observations —
        never substituted zeros (§7.4 / ODD data note)."""
        await _seed_wellness(
            db_session, hrv_rmssd=None, resting_hr=None, sleep_minutes=None
        )
        await _seed_tsb(db_session)
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.rows_upserted == DAYS
        row = (
            (
                await db_session.execute(
                    _fresh(
                        select(ReadinessSnapshotRow).where(
                            ReadinessSnapshotRow.date == WINDOW_END
                        )
                    )
                )
            )
            .scalar_one()
        )
        assert all(s["status"] == "insufficient_data" for s in row.signals)
        assert all(s["observed"] is None for s in row.signals)
        assert all(s["baseline_mean"] is None for s in row.signals)
        assert row.suggest_reduce_intensity is False

    async def test_day_without_tsb_row_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        """No ``daily_load`` ``combined`` row -> no TSB context -> the day
        is reported as skipped, never assessed with a fabricated TSB."""
        await _seed_wellness(db_session)
        await _seed_tsb(db_session)
        # Remove the TSB row of the middle window day.
        middle = WINDOW_END - dt.timedelta(days=1)
        await db_session.execute(
            delete(DailyLoadRow).where(
                DailyLoadRow.date == middle,
                DailyLoadRow.sport == "combined",
            )
        )
        await db_session.commit()

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.rows_upserted == DAYS - 1
        assert [(s.date, s.reason) for s in report.skipped] == [
            (middle, report.skipped[0].reason)
        ]
        assert "daily_load" in report.skipped[0].reason
        dates = {
            r.date
            for r in (
                await db_session.execute(_fresh(select(ReadinessSnapshotRow)))
            )
            .scalars()
        }
        assert middle not in dates
        assert len(dates) == DAYS - 1
