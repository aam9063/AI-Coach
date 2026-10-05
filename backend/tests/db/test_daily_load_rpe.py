"""RPE-through-the-read-layer tests (ODD LOAD-12).

The stored owner-entered ``activity.rpe`` must reach the engine so a
strength session with an RPE gets a real sRPE load (instead of being
skipped or mis-rated through gym-level HR), and the configured sRPE
factor (``engine_srpe_tss_equivalent_factor``; placeholder 1.0, LOAD-11)
must flow into the persisted and returned load. The chosen method stays
traceable in the persisted ``methods`` trace and the CLI report.
"""

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.daily_load import format_report
from app.db.models import ActivityRow, DailyLoadRow
from app.engine.load import ThresholdBundle, TrimpCoefficients
from app.services.daily_load import (
    COMBINED_SPORT_KEY,
    DailyLoadReport,
    build_activity_load_input,
    recompute_daily_load,
)

pytestmark = pytest.mark.anyio

DAY = dt.date(2026, 7, 1)
START = dt.datetime(2026, 7, 1, 8, 0, tzinfo=dt.UTC)
COEFFS = TrimpCoefficients.from_sex("male")
ENGINE_VERSION = "test-load12"
HR_REST, HR_MAX, LTHR = 65.0, 186.0, 169.0


def _thresholds(factor: float = 0.5) -> ThresholdBundle:
    return ThresholdBundle(
        lthr_bpm=LTHR,
        hr_max_bpm=HR_MAX,
        hr_rest_bpm=HR_REST,
        srpe_tss_equivalent_factor=factor,
    )


def _strength_row(*, rpe: float | None = None) -> ActivityRow:
    return ActivityRow(
        source="intervals",
        source_id="i-strength",
        type="WeightTraining",
        name="Gym",
        start_time=START,
        duration_s=3600,
        rpe=rpe,
    )


class TestBuildActivityLoadInput:
    def test_stored_rpe_is_passed_through(self) -> None:
        load_input, reason = build_activity_load_input(
            _strength_row(rpe=7), streams={}
        )
        assert reason is None
        assert load_input is not None
        assert load_input.rpe == 7

    def test_null_rpe_stays_none(self) -> None:
        load_input, _ = build_activity_load_input(_strength_row(rpe=None), streams={})
        assert load_input is not None
        assert load_input.rpe is None


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _store_strength_session(
    db_session: AsyncSession,
    *,
    source_id: str,
    rpe: float | None,
    hr_payload: list[float | None] | None = None,
) -> None:
    activity = await repository.upsert_activity(
        db_session,
        source_id=source_id,
        type="WeightTraining",
        name="Gym",
        start_time=START,
        duration_s=3600,
        rpe=rpe,
    )
    if hr_payload is not None:
        from typing import Any, cast

        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="hr",
            data=cast(list[float], cast(Any, hr_payload)),
        )


async def _recompute(
    db_session: AsyncSession, *, factor: float = 0.5
) -> DailyLoadReport:
    return await recompute_daily_load(
        db_session,
        athlete_id=1,
        window_end=DAY,
        days=1,
        thresholds=_thresholds(factor),
        coefficients=COEFFS,
        engine_version=ENGINE_VERSION,
    )


class TestStrengthSrpeRecompute:
    async def test_strength_with_rpe_gets_real_srpe_load(
        self, db_session: AsyncSession
    ) -> None:
        """Gym-level HR (95 bpm — the owner's real average) would be
        near-rest for TRIMP; the RPE must win for strength sports."""
        await _store_strength_session(
            db_session, source_id="i-srpe-1", rpe=7, hr_payload=[95.0] * 10
        )

        report = await _recompute(db_session)
        await db_session.commit()

        assert report.skipped == ()
        row = (
            await db_session.execute(
                _fresh(select(DailyLoadRow).where(DailyLoadRow.sport == "weighttraining"))
            )
        ).scalar_one()
        # 7 (RPE) x 60 min x 0.5 (configured factor) = 210
        assert row.tss == pytest.approx(210.0)
        assert row.methods == {"srpe": 1}
        assert row.engine_version == ENGINE_VERSION

    async def test_configured_factor_flows_into_the_load(
        self, db_session: AsyncSession
    ) -> None:
        await _store_strength_session(db_session, source_id="i-srpe-2", rpe=7)
        report = await _recompute(db_session, factor=0.7)
        await db_session.commit()
        row = (
            await db_session.execute(
                _fresh(select(DailyLoadRow).where(DailyLoadRow.sport == "weighttraining"))
            )
        ).scalar_one()
        assert row.tss == pytest.approx(7 * 60 * 0.7)
        assert row.methods == {"srpe": 1}
        assert report.methods_used == {"srpe": 1}
        combined = (
            await db_session.execute(
                _fresh(
                    select(DailyLoadRow).where(
                        DailyLoadRow.sport == COMBINED_SPORT_KEY
                    )
                )
            )
        ).scalar_one()
        assert combined.tss == pytest.approx(7 * 60 * 0.7)
    async def test_strength_without_rpe_still_uses_hr(
        self, db_session: AsyncSession
    ) -> None:
        await _store_strength_session(
            db_session, source_id="i-srpe-3", rpe=None, hr_payload=[150.0] * 10
        )
        report = await _recompute(db_session)
        await db_session.commit()
        row = (
            await db_session.execute(
                _fresh(select(DailyLoadRow).where(DailyLoadRow.sport == "weighttraining"))
            )
        ).scalar_one()
        assert row.methods == {"hr": 1}
        assert row.tss == pytest.approx(60.4576391242)
        assert report.methods_used == {"hr": 1}

    async def test_strength_without_rpe_or_hr_is_skipped_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        await _store_strength_session(db_session, source_id="i-srpe-4", rpe=None)
        report = await _recompute(db_session)
        await db_session.commit()
        assert len(report.skipped) == 1
        assert "no applicable load method" in report.skipped[0].reason
        assert "no RPE recorded" in report.skipped[0].reason

    async def test_report_names_the_srpe_method(
        self, db_session: AsyncSession
    ) -> None:
        await _store_strength_session(db_session, source_id="i-srpe-5", rpe=7)
        report = await _recompute(db_session)
        await db_session.commit()
        text = format_report(report)
        assert "srpe" in text
