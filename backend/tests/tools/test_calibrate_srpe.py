"""Calibration-tool tests (ODD LOAD-12).

``app.tools.calibrate_srpe`` computes the implied sRPE TSS-equivalent
factor as the MEDIAN of ``hrTSS / sRPE_AU`` over sessions that have BOTH
a stored owner-entered RPE and an HR stream — this is how the documented
placeholder factor (1.0, LOAD-11) gets replaced by a measured one once
RPE data exists. With no RPE data it must report zero sessions instead of
failing or inventing a number. The tool is strictly read-only.
"""

import datetime as dt
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import ActivityRow
from app.engine.load import ThresholdBundle, TrimpCoefficients, hrtss, trimp
from app.tools.calibrate_srpe import (
    CalibrationReport,
    ExcludedSession,
    SrpeHrSession,
    collect_srpe_hr_sessions,
    format_report,
    implied_srpe_factor,
)

pytestmark = pytest.mark.anyio

HR_REST, HR_MAX, LTHR = 65.0, 186.0, 169.0
COEFFS = TrimpCoefficients.from_sex("male")
THRESHOLDS = ThresholdBundle(
    lthr_bpm=LTHR, hr_max_bpm=HR_MAX, hr_rest_bpm=HR_REST
)

START = dt.datetime(2026, 7, 1, 8, 0, tzinfo=dt.UTC)


def _engine_hr_tss(hr_avg: float, duration_s: float) -> float:
    """The same hrTSS through the pure engine, for parity assertions."""
    return hrtss(
        trimp(duration_s / 60.0, hr_avg, HR_REST, HR_MAX, coefficients=COEFFS),
        trimp(
            60.0,
            LTHR,
            HR_REST,
            HR_MAX,
            coefficients=COEFFS,
        ),
    )


def _session(hr_tss: float, rpe: float, duration_min: float) -> SrpeHrSession:
    return SrpeHrSession(
        activity_id=1,
        sport="WeightTraining",
        rpe=rpe,
        duration_min=duration_min,
        hr_avg_bpm=150.0,
        hr_tss=hr_tss,
        srpe_au=rpe * duration_min,
    )


class TestImpliedFactor:
    def test_single_session_is_its_own_ratio(self) -> None:
        tss = 60.4576391242
        factor = implied_srpe_factor([_session(tss, rpe=7, duration_min=60.0)])
        assert factor == pytest.approx(tss / 420.0)

    def test_median_of_three_sessions(self) -> None:
        ratios = [10.0, 20.0, 30.0]  # hrTSS == sRPE_AU * ratio
        sessions = [
            _session(ratio * au, rpe=7, duration_min=au / 7.0)
            for ratio, au in zip(ratios, [420.0, 420.0, 420.0], strict=True)
        ]
        assert implied_srpe_factor(sessions) == pytest.approx(20.0)

    def test_median_of_even_count_averages_the_middle_pair(self) -> None:
        sessions = [
            _session(1.0 * 420.0, rpe=7, duration_min=60.0),
            _session(4.0 * 420.0, rpe=7, duration_min=60.0),
        ]
        assert implied_srpe_factor(sessions) == pytest.approx(2.5)

    def test_no_sessions_is_none_never_a_number(self) -> None:
        assert implied_srpe_factor([]) is None


class TestCalibrationReport:
    def test_report_carries_sessions_and_median(self) -> None:
        sessions = (
            _session(60.4576391242, rpe=7, duration_min=60.0),
        )
        report = CalibrationReport(sessions=sessions, excluded=())
        assert report.implied_factor == pytest.approx(60.4576391242 / 420.0)

    def test_zero_session_report_states_it(self) -> None:
        report = CalibrationReport(sessions=(), excluded=())
        text = format_report(report)
        assert "0" in text
        assert "no sessions" in text.lower() or "zero" in text.lower()

    def test_report_states_the_calibration_purpose(self) -> None:
        text = format_report(CalibrationReport(sessions=(), excluded=()))
        # The report must say this is how the placeholder factor gets
        # replaced by a measured one.
        assert "placeholder" in text.lower()

    def test_excluded_sessions_are_reported_not_silent(self) -> None:
        report = CalibrationReport(
            sessions=(),
            excluded=(ExcludedSession(2, "WeightTraining", "no HR stream"),),
        )
        text = format_report(report)
        assert "2" in text
        assert "no HR stream" in text


class TestCollectSessions:
    async def _store_gym_session(
        self,
        db_session: AsyncSession,
        *,
        source_id: str,
        rpe: float | None,
        duration_s: int = 3600,
        hr_payload: list[float | None] | None = None,
        type_: str = "WeightTraining",
    ) -> ActivityRow:
        activity = await repository.upsert_activity(
            db_session,
            source_id=source_id,
            type=type_,
            name="Gym",
            start_time=START,
            duration_s=duration_s,
            rpe=rpe,
        )
        if hr_payload is not None:
            await repository.upsert_activity_stream(
                db_session,
                activity_id=activity.id,
                stream_type="hr",
                data=cast(list[float], cast(Any, hr_payload)),
            )
        return activity

    async def test_collects_sessions_with_both_rpe_and_hr(
        self, db_session: AsyncSession
    ) -> None:
        await self._store_gym_session(
            db_session, source_id="i-cal-1", rpe=7, hr_payload=[150.0] * 10
        )
        await self._store_gym_session(
            db_session,
            source_id="i-cal-2",
            rpe=8,
            duration_s=1800,
            hr_payload=[170.0] * 10,
        )

        report = await collect_srpe_hr_sessions(
            db_session, thresholds=THRESHOLDS, coefficients=COEFFS
        )

        assert len(report.sessions) == 2
        first = report.sessions[0]
        assert first.rpe == 7
        assert first.duration_min == 60.0
        assert first.hr_avg_bpm == 150.0
        assert first.hr_tss == pytest.approx(_engine_hr_tss(150.0, 3600.0))
        assert first.srpe_au == pytest.approx(420.0)
        expected_median = (
            _engine_hr_tss(150.0, 3600.0) / 420.0
            + _engine_hr_tss(170.0, 1800.0) / (8 * 30.0)
        ) / 2.0
        assert report.implied_factor == pytest.approx(expected_median)

    async def test_rpe_without_hr_stream_is_excluded_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        await self._store_gym_session(db_session, source_id="i-cal-3", rpe=7)

        report = await collect_srpe_hr_sessions(
            db_session, thresholds=THRESHOLDS, coefficients=COEFFS
        )

        assert report.sessions == ()
        assert report.implied_factor is None
        assert len(report.excluded) == 1
        assert "HR" in report.excluded[0].reason

    async def test_activities_without_rpe_are_not_candidates(
        self, db_session: AsyncSession
    ) -> None:
        await self._store_gym_session(
            db_session, source_id="i-cal-4", rpe=None, hr_payload=[150.0] * 10
        )

        report = await collect_srpe_hr_sessions(
            db_session, thresholds=THRESHOLDS, coefficients=COEFFS
        )

        assert report.sessions == ()
        assert report.implied_factor is None
        # Not an error, not a number: zero sessions, reported.

    async def test_unconfigured_hr_thresholds_exclude_with_reason(
        self, db_session: AsyncSession
    ) -> None:
        await self._store_gym_session(
            db_session, source_id="i-cal-5", rpe=7, hr_payload=[150.0] * 10
        )
        empty = ThresholdBundle()  # no LTHR/max/rest configured

        report = await collect_srpe_hr_sessions(
            db_session, thresholds=empty, coefficients=COEFFS
        )

        assert report.sessions == ()
        assert report.implied_factor is None
        assert len(report.excluded) == 1
        assert "configured" in report.excluded[0].reason.lower()

    async def test_zero_session_db_report_is_reported(
        self, db_session: AsyncSession
    ) -> None:
        report = await collect_srpe_hr_sessions(
            db_session, thresholds=THRESHOLDS, coefficients=COEFFS
        )
        assert report.sessions == ()
        assert report.implied_factor is None
        text = format_report(report)
        assert "0" in text

    async def test_only_rpe_activities_are_considered(
        self, db_session: AsyncSession
    ) -> None:
        """A ride with HR but no RPE must not enter the calibration set
        (sRPE calibration uses strength sessions with both values)."""
        await repository.upsert_activity(
            db_session,
            source_id="i-cal-6",
            type="Ride",
            name="Ride",
            start_time=START,
            duration_s=3600,
            rpe=None,
        )
        activity = (
            await db_session.execute(
                select(ActivityRow).where(ActivityRow.source_id == "i-cal-6")
            )
        ).scalar_one()
        await repository.upsert_activity_stream(
            db_session,
            activity_id=activity.id,
            stream_type="hr",
            data=cast(list[float], cast(Any, [150.0] * 10)),
        )

        report = await collect_srpe_hr_sessions(
            db_session, thresholds=THRESHOLDS, coefficients=COEFFS
        )
        assert report.sessions == ()
