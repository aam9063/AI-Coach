"""Settings-sourced engine constants flow into the persistence service
(LOAD-11): non-default values injected through the read layer provably
change the persisted output, which then matches the pure engine.

Requires the compose Postgres; skips cleanly without it (see conftest).
"""

from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import DailyLoadRow
from app.engine.load import (
    ActivityLoadInput,
    ThresholdBundle,
    TrimpCoefficients,
    select_load_method,
)
from app.engine.pmc import compute_pmc_per_sport
from app.services.daily_load import recompute_daily_load

pytestmark = pytest.mark.anyio

ATHLETE_ID = 1
HR_REST, HR_MAX, LTHR = 65.0, 186.0, 169.0
COEFFS = TrimpCoefficients.from_sex("male")
ENGINE_VERSION = "test-load11"
DAY = date(2026, 7, 1)
START = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def _store(
    db_session: AsyncSession,
    *,
    source_id: str,
    hr_payload: list[float | None] | None = None,
    power_payload: list[float | None] | None = None,
) -> None:
    from app.db import repository

    activity = await repository.upsert_activity(
        db_session,
        source_id=source_id,
        type="Ride",
        name="Ride",
        start_time=START,
        duration_s=3600,
    )
    for stream_type, payload in (("hr", hr_payload), ("power", power_payload)):
        if payload is not None:
            await repository.upsert_activity_stream(
                db_session,
                activity_id=activity.id,
                stream_type=stream_type,
                data=cast(list[float], payload),
            )


async def _rows(db_session: AsyncSession, sport: str) -> list[DailyLoadRow]:
    result = await db_session.execute(
        select(DailyLoadRow)
        .where(DailyLoadRow.sport == sport)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


HR_PAYLOAD: list[float | None] = [150.0] * 4  # gaps excluded -> mean 150.0


async def _recompute(db_session: AsyncSession, **kwargs: Any) -> None:
    await recompute_daily_load(
        db_session,
        athlete_id=ATHLETE_ID,
        window_end=DAY,
        days=1,
        thresholds=ThresholdBundle(lthr_bpm=LTHR, hr_max_bpm=HR_MAX, hr_rest_bpm=HR_REST),
        coefficients=COEFFS,
        engine_version=ENGINE_VERSION,
        **kwargs,
    )
    await db_session.commit()


class TestPmcTimeConstantsFlow:
    async def test_non_default_tau_ctl_changes_ctl_to_the_engine_value(
        self, db_session: AsyncSession
    ) -> None:
        await _store(db_session, source_id="i-tau-1", hr_payload=HR_PAYLOAD)

        await _recompute(db_session)
        default_ctl = (await _rows(db_session, "ride"))[0].ctl
        assert default_ctl > 0.0

        await _recompute(db_session, tau_ctl_days=14.0)
        fast_ctl = (await _rows(db_session, "ride"))[0].ctl
        assert fast_ctl != pytest.approx(default_ctl)

        expected = compute_pmc_per_sport(
            {"ride": {DAY: _expected_tss()}}, tau_ctl_days=14.0
        )
        assert fast_ctl == pytest.approx(expected.per_sport["ride"].days[0].ctl)


def _expected_tss() -> float:
    """The engine's hrTSS for the stored activity (parity anchor)."""
    selection = select_load_method(
        ActivityLoadInput(sport="Ride", duration_s=3600.0, hr_avg_bpm=150.0),
        ThresholdBundle(lthr_bpm=LTHR, hr_max_bpm=HR_MAX, hr_rest_bpm=HR_REST),
        coefficients=COEFFS,
    )
    return selection.tss


class TestTrimpReferenceMinutesFlow:
    async def test_halved_reference_doubles_hrtss(
        self, db_session: AsyncSession
    ) -> None:
        await _store(db_session, source_id="i-ref-1", hr_payload=HR_PAYLOAD)

        await _recompute(db_session)
        default_tss = (await _rows(db_session, "ride"))[0].tss

        await _recompute(db_session, trimp_reference_minutes=30.0)
        halved_tss = (await _rows(db_session, "ride"))[0].tss

        # TRIMP is linear in duration: a 30-minute reference exactly doubles hrTSS.
        assert halved_tss == pytest.approx(2.0 * default_tss)
        assert default_tss == pytest.approx(_expected_tss())


class TestNpWindowFlow:
    async def test_non_default_np_window_changes_power_tss(
        self, db_session: AsyncSession
    ) -> None:
        ramp = [float(i) for i in range(60)]
        await _store(
            db_session, source_id="i-np-1", power_payload=ramp + [None] * 3
        )
        thresholds = ThresholdBundle(
            ftp_watts=200.0, lthr_bpm=LTHR, hr_max_bpm=HR_MAX, hr_rest_bpm=HR_REST
        )

        await recompute_daily_load(
            db_session,
            athlete_id=ATHLETE_ID,
            window_end=DAY,
            days=1,
            thresholds=thresholds,
            coefficients=COEFFS,
            engine_version=ENGINE_VERSION,
        )
        await db_session.commit()
        default_tss = (await _rows(db_session, "ride"))[0].tss

        await recompute_daily_load(
            db_session,
            athlete_id=ATHLETE_ID,
            window_end=DAY,
            days=1,
            thresholds=thresholds,
            coefficients=COEFFS,
            engine_version=ENGINE_VERSION,
            np_window_samples=60,
        )
        await db_session.commit()
        wide_tss = (await _rows(db_session, "ride"))[0].tss

        assert wide_tss != pytest.approx(default_tss)
        # The wide-window value equals the pure engine with the same window.
        expected = select_load_method(
            ActivityLoadInput(
                sport="Ride",
                duration_s=3600.0,
                power_samples=ramp + [None] * 3,
            ),
            thresholds,
            coefficients=COEFFS,
            np_window_samples=60,
        )
        assert wide_tss == pytest.approx(expected.tss)
