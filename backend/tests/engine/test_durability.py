"""Durability: Efficiency Factor, aerobic decoupling (Pa:HR) and long-session
trends (RID-8/RID-9, PROJECT_BRIEF section 7.6).

Hand-calculated anchors (section 12.5 acceptance): the decoupling example
below is computed BY HAND in the test docstring and asserted exactly.
"""

import datetime as dt

import pytest

from app.engine.durability import (
    DEFAULT_DECOUPLING_REFERENCE_BAND,
    DEFAULT_MAX_HALF_INTENSITY_DRIFT,
    DEFAULT_MIN_LONG_SESSION_SECONDS,
    DEFAULT_MIN_TREND_SESSIONS,
    DEFAULT_TREND_WINDOW_DAYS,
    DurabilitySessionPoint,
    aerobic_decoupling,
    bike_efficiency_factor,
    durability_trend,
    efficiency_factor,
    run_efficiency_factor,
    session_decoupling,
)
from app.engine.load import normalized_graded_speed, normalized_power


def _bike_stream(
    power_first: float,
    power_second: float,
    hr_first: float,
    hr_second: float,
    half: int,
) -> tuple[list[float], list[float]]:
    """Per-second power and HR streams: constant per half, ``half`` samples each."""
    return (
        [power_first] * half + [power_second] * half,
        [hr_first] * half + [hr_second] * half,
    )


# ---------------------------------------------------------------------------
# RID-8: Efficiency Factor
# ---------------------------------------------------------------------------


class TestEfficiencyFactor:
    def test_bike_ef_hand_computed(self) -> None:
        """EF = NP / avg HR: 200 W / 150 bpm = 1.3333... W per bpm."""
        power = [200.0] * 60
        hr = [150.0] * 60
        assert bike_efficiency_factor(power, hr_samples=hr) == pytest.approx(
            200.0 / 150.0
        )

    def test_run_ef_hand_computed(self) -> None:
        """EF = NGS / avg HR: 3.0 m/s / 150 bpm = 0.02 (m/s) per bpm."""
        speed = [3.0] * 60
        hr = [150.0] * 60
        assert run_efficiency_factor(speed, hr_samples=hr) == pytest.approx(
            3.0 / 150.0
        )

    def test_bike_ef_reuses_normalized_power(self) -> None:
        """The bike EF reuses the engine's NP rather than reimplementing it."""
        power: list[float | None] = (
            [150.0] * 20 + [None] + [250.0] * 20 + [180.0] * 20
        )
        hr = [140.0 + i % 10 for i in range(61)]
        expected = normalized_power(power) / (
            sum(h for h in hr if h is not None) / len(hr)
        )
        assert bike_efficiency_factor(power, hr_samples=hr) == pytest.approx(
            expected, rel=1e-12
        )

    def test_run_ef_reuses_normalized_graded_speed(self) -> None:
        """The run EF reuses the engine's NGS (Minetti grade model) as-is."""
        speed: list[float | None] = [3.0 + 0.05 * (i % 4) for i in range(40)]
        distance = [float(i) for i in range(40)]
        altitude = [0.5 * i for i in range(40)]
        hr = [155.0] * 40
        expected = normalized_graded_speed(
            speed, distance_samples=distance, altitude_samples=altitude
        ) / 155.0
        assert run_efficiency_factor(
            speed,
            hr_samples=hr,
            distance_samples=distance,
            altitude_samples=altitude,
        ) == pytest.approx(expected, rel=1e-12)

    def test_efficiency_factor_rejects_non_positive_hr(self) -> None:
        with pytest.raises(ValueError, match="HR"):
            efficiency_factor(200.0, 0.0)
        with pytest.raises(ValueError, match="HR"):
            efficiency_factor(200.0, -150.0)

    def test_efficiency_factor_rejects_non_positive_normalized_value(self) -> None:
        with pytest.raises(ValueError, match="normalized"):
            efficiency_factor(0.0, 150.0)
        with pytest.raises(ValueError, match="normalized"):
            efficiency_factor(-200.0, 150.0)

    def test_bike_ef_rejects_misaligned_streams(self) -> None:
        with pytest.raises(ValueError, match="length"):
            bike_efficiency_factor([200.0] * 30, hr_samples=[150.0] * 29)

    def test_bike_ef_rejects_all_missing_hr(self) -> None:
        with pytest.raises(ValueError, match="HR"):
            bike_efficiency_factor([200.0] * 30, hr_samples=[None] * 30)


# ---------------------------------------------------------------------------
# RID-8: aerobic decoupling (Pa:HR)
# ---------------------------------------------------------------------------


class TestAerobicDecouplingCore:
    def test_sign_convention_positive_is_deterioration(self) -> None:
        """Second-half EF below first-half EF -> POSITIVE decoupling."""
        value = aerobic_decoupling(1.3333333333333333, 1.2)
        assert value > 0.0

    def test_sign_convention_negative_is_improvement(self) -> None:
        """Second-half EF above first-half EF -> NEGATIVE decoupling."""
        assert aerobic_decoupling(1.2, 1.3333333333333333) < 0.0

    def test_equal_halves_give_zero(self) -> None:
        assert aerobic_decoupling(1.25, 1.25) == 0.0

    def test_rejects_non_positive_ef(self) -> None:
        with pytest.raises(ValueError, match="EF"):
            aerobic_decoupling(0.0, 1.2)
        with pytest.raises(ValueError, match="EF"):
            aerobic_decoupling(1.2, 0.0)


class TestSessionDecouplingHandCalculated:
    def test_hand_calculated_decoupling_above_reference_band(self) -> None:
        """MANDATORY hand-calculated example (section 12.5 acceptance).

        Session (bike, 600 s at 1 Hz), constant per half:

            first half : 300 samples at 200 W, HR 150 bpm
            second half: 300 samples at 190 W, HR 155 bpm

        Hand arithmetic:

            EF1 = NP1 / HR1 = 200 / 150 = 1.3333333... W/bpm
            EF2 = NP2 / HR2 = 190 / 155 = 1.2258064... W/bpm

            Pa:HR = (EF1 - EF2) / EF1
                  = (1.3333333 - 1.2258065) / 1.3333333
                  = 0.1075269 / 1.3333333
                  = 0.0806452  (= 5/62 exactly: 1 - 28500/31000)

                  = 8.0645% -> ABOVE the 5% Friel reference band, so
                  within_reference_band is False.

        Sign: EF FELL in the second half -> positive decoupling (the classic
        aerobic decoupling / cardiac drift pattern).
        """
        power, hr = _bike_stream(200.0, 190.0, 150.0, 155.0, 300)
        result = session_decoupling("bike", power, hr_samples=hr)
        assert result.status == "ok"
        assert result.ef_first_half == pytest.approx(200.0 / 150.0)
        assert result.ef_second_half == pytest.approx(190.0 / 155.0)
        assert result.decoupling == pytest.approx(5.0 / 62.0, rel=1e-9)
        assert result.decoupling_pct == pytest.approx(100.0 * 5.0 / 62.0, rel=1e-9)
        assert result.decoupling is not None
        assert result.decoupling > 0.0  # deteriorated in the second half
        assert result.within_reference_band is False
        assert result.intensity_drift == pytest.approx(10.0 / 200.0)

    def test_below_reference_band(self) -> None:
        """EF1 200/150 = 1.3333333, EF2 196/150 = 1.3066667:
        (1.3333333 - 1.3066667) / 1.3333333 = 0.02 = 2% < 5% -> within band."""
        power, hr = _bike_stream(200.0, 196.0, 150.0, 150.0, 300)
        result = session_decoupling("bike", power, hr_samples=hr)
        assert result.decoupling == pytest.approx(0.02)
        assert result.within_reference_band is True

    def test_exactly_five_percent_boundary_is_beyond_the_band(self) -> None:
        """Strict boundary convention (ZON-9 / readiness precedents).

        EF1 = 200/150, EF2 = 190/150: (1.3333333 - 1.2666667)/1.3333333
        = 0.05 exactly. A decoupling AT 5% is NOT reported as within the
        reference band: the band is strict (value must be strictly below
        5%), compared tolerantly so float dust cannot flip the outcome.
        """
        power, hr = _bike_stream(200.0, 190.0, 150.0, 150.0, 300)
        result = session_decoupling("bike", power, hr_samples=hr)
        assert result.decoupling == pytest.approx(0.05, rel=1e-9)
        assert result.within_reference_band is False

    def test_run_decoupling_improving_session_is_negative(self) -> None:
        """Constant 3.0 m/s; HR falls 160 -> 150: EF RISES -> negative value."""
        speed = [3.0] * 600
        hr = [160.0] * 300 + [150.0] * 300
        result = session_decoupling("run", speed, hr_samples=hr)
        assert result.status == "ok"
        assert result.decoupling is not None and result.decoupling < 0.0

    def test_odd_sample_count_first_half_gets_the_extra_sample(self) -> None:
        """5 samples -> first half 3, second half 2 (documented split rule).

        Run, constant 3.0 m/s; HR [150, 150, 150, 160, 160]:
        EF1 = 3/150, EF2 = 3/160 -> positive decoupling, and the reported
        half sizes expose the extra sample landing in the FIRST half.
        """
        speed = [3.0] * 5
        hr = [150.0, 150.0, 150.0, 160.0, 160.0]
        result = session_decoupling("run", speed, hr_samples=hr)
        assert result.n_samples == 5
        assert result.n_first_half == 3
        assert result.n_second_half == 2
        assert result.decoupling == pytest.approx(
            (3.0 / 150.0 - 3.0 / 160.0) / (3.0 / 150.0)
        )

    def test_reference_band_is_configurable(self) -> None:
        power, hr = _bike_stream(200.0, 196.0, 150.0, 150.0, 300)
        result = session_decoupling(
            "bike", power, hr_samples=hr, reference_band=0.01
        )
        assert result.decoupling == pytest.approx(0.02)
        assert result.within_reference_band is False

    def test_default_reference_band_is_friel_five_percent(self) -> None:
        assert DEFAULT_DECOUPLING_REFERENCE_BAND == 0.05


class TestSteadinessGuard:
    def test_steady_session_passes_default_guard(self) -> None:
        """NP halves 200 W vs 190 W: drift 5% < 15% default -> steady."""
        power, hr = _bike_stream(200.0, 190.0, 150.0, 155.0, 300)
        result = session_decoupling("bike", power, hr_samples=hr)
        assert result.status == "ok"
        assert result.max_intensity_drift == DEFAULT_MAX_HALF_INTENSITY_DRIFT

    def test_unsteady_session_is_rejected_without_a_decoupling_value(self) -> None:
        """NP halves 200 W vs 160 W: drift 20% > 15% -> not_steady.

        The decoupling is NOT reported (None): the module refuses to publish
        a Pa:HR the steadiness guard says is uninterpretable.
        """
        power, hr = _bike_stream(200.0, 160.0, 150.0, 155.0, 300)
        result = session_decoupling("bike", power, hr_samples=hr)
        assert result.status == "not_steady"
        assert result.decoupling is None
        assert result.ef_first_half is None
        assert result.intensity_drift == pytest.approx(40.0 / 200.0)
        assert "steady" in result.detail.lower()

    def test_drift_exactly_at_limit_counts_as_steady(self) -> None:
        """Drift exactly AT the limit (0.05 with limit 0.05) is steady:
        the guard rejects only strictly beyond the limit (tolerant boundary)."""
        power, hr = _bike_stream(200.0, 190.0, 150.0, 155.0, 300)
        result = session_decoupling(
            "bike", power, hr_samples=hr, max_intensity_drift=0.05
        )
        assert result.status == "ok"

    def test_drift_limit_is_symmetric_in_sign(self) -> None:
        """A harder second half (NP 200 -> 210, drift 5%) trips the same
        guard: the guard measures |drift|, not direction."""
        power, hr = _bike_stream(200.0, 210.0, 150.0, 155.0, 300)
        result = session_decoupling(
            "bike", power, hr_samples=hr, max_intensity_drift=0.04
        )
        assert result.status == "not_steady"

    def test_run_guard_uses_ngs_halves(self) -> None:
        """Run steadiness compares the NGS of the two halves."""
        speed = [3.0] * 300 + [4.0] * 300
        hr = [150.0] * 600
        result = session_decoupling(
            "run", speed, hr_samples=hr, max_intensity_drift=0.1
        )
        assert result.status == "not_steady"
        assert result.intensity_drift == pytest.approx(1.0 / 3.0)


class TestSessionDecouplingValidation:
    def test_rejects_misaligned_streams(self) -> None:
        with pytest.raises(ValueError, match="length"):
            session_decoupling("bike", [200.0] * 10, hr_samples=[150.0] * 9)

    def test_rejects_single_sample_session(self) -> None:
        with pytest.raises(ValueError, match="half"):
            session_decoupling("bike", [200.0], hr_samples=[150.0])

    def test_rejects_empty_streams(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            session_decoupling("bike", [], hr_samples=[])

    def test_rejects_unknown_sport(self) -> None:
        with pytest.raises(ValueError, match="sport"):
            session_decoupling("swim", [1.0] * 10, hr_samples=[150.0] * 10)  # type: ignore[arg-type]

    def test_rejects_half_without_usable_hr(self) -> None:
        with pytest.raises(ValueError, match="HR"):
            session_decoupling("bike", [200.0] * 4, hr_samples=[150.0, None, None, None])

    def test_rejects_invalid_guard_or_band(self) -> None:
        power, hr = _bike_stream(200.0, 190.0, 150.0, 155.0, 10)
        with pytest.raises(ValueError, match="drift"):
            session_decoupling("bike", power, hr_samples=hr, max_intensity_drift=-0.1)
        with pytest.raises(ValueError, match="band"):
            session_decoupling("bike", power, hr_samples=hr, reference_band=0.0)
        with pytest.raises(ValueError, match="band"):
            session_decoupling("bike", power, hr_samples=hr, reference_band=1.5)


# ---------------------------------------------------------------------------
# RID-9: durability trends on long sessions (Maunder et al. 2021)
# ---------------------------------------------------------------------------


def _point(
    days_ago: int, duration_s: float, decoupling: float | None, *, end: dt.date
) -> DurabilitySessionPoint:
    return DurabilitySessionPoint(
        session_date=end - dt.timedelta(days=days_ago),
        duration_s=duration_s,
        decoupling=decoupling,
    )


class TestDurabilityTrend:
    def test_default_long_threshold_and_minimums_are_documented_choices(self) -> None:
        """90 min 'long' threshold (OWNER CHOICE), 3-session minimum (OWNER
        CHOICE), 84-day window (OWNER CHOICE): provenance in the module."""
        assert DEFAULT_MIN_LONG_SESSION_SECONDS == 5400.0
        assert DEFAULT_MIN_TREND_SESSIONS == 3
        assert DEFAULT_TREND_WINDOW_DAYS == 84

    def test_monotone_worsening_series_has_positive_slope(self) -> None:
        """Weekly long sessions, decoupling 3% -> 6%: slope +0.01/7 per day."""
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(21, 7200.0, 0.03, end=end),
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.05, end=end),
            _point(0, 7200.0, 0.06, end=end),
        ]
        result = durability_trend(sessions)
        assert result.status == "trend"
        assert result.slope_per_day == pytest.approx(0.01 / 7.0)
        assert result.direction == "worsening"
        assert result.n_eligible == 4
        assert result.n_ineligible == 0

    def test_monotone_improving_series_has_negative_slope(self) -> None:
        """Decoupling 6% -> 3% over four weekly long sessions: falling Pa:HR
        = the athlete holds EF longer = improving durability."""
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(21, 7200.0, 0.06, end=end),
            _point(14, 7200.0, 0.05, end=end),
            _point(7, 7200.0, 0.04, end=end),
            _point(0, 7200.0, 0.03, end=end),
        ]
        result = durability_trend(sessions)
        assert result.status == "trend"
        assert result.slope_per_day == pytest.approx(-0.01 / 7.0)
        assert result.direction == "improving"

    def test_constant_series_is_stable(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.04, end=end),
            _point(0, 7200.0, 0.04, end=end),
        ]
        result = durability_trend(sessions)
        assert result.slope_per_day == pytest.approx(0.0)
        assert result.direction == "stable"

    def test_short_sessions_are_ineligible_with_reason(self) -> None:
        """Sessions below the 90-min threshold are reported as ineligible
        with the reason and can never silently enter the trend."""
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(21, 7200.0, 0.03, end=end),
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.05, end=end),
            _point(0, 7200.0, 0.06, end=end),
            # An extreme 45-min session: if it leaked into the slope it
            # would drag the trend violently upward.
            _point(3, 2700.0, 0.50, end=end),
        ]
        result = durability_trend(sessions)
        assert result.status == "trend"
        assert result.n_eligible == 4
        assert result.n_ineligible == 1
        assert result.ineligible[0].reason == "below_min_duration"
        # The slope is IDENTICAL to the clean series: the short session
        # cannot silently enter the trend.
        assert result.slope_per_day == pytest.approx(0.01 / 7.0)

    def test_duration_exactly_at_threshold_is_eligible(self) -> None:
        """Inclusive boundary: 5400 s IS a long session (tolerant boundary)."""
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(14, 5400.0, 0.03, end=end),
            _point(7, 5400.0, 0.04, end=end),
            _point(0, 5400.0, 0.05, end=end),
        ]
        result = durability_trend(sessions)
        assert result.n_eligible == 3
        assert result.n_ineligible == 0

    def test_too_few_eligible_sessions_is_insufficient_data(self) -> None:
        """Two eligible sessions (minimum is 3): explicit insufficient_data
        with counts — never a trend from one point, never a zero."""
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(7, 7200.0, 0.03, end=end),
            _point(0, 7200.0, 0.06, end=end),
        ]
        result = durability_trend(sessions)
        assert result.status == "insufficient_data"
        assert result.slope_per_day is None
        assert result.direction is None
        assert result.n_eligible == 2
        assert result.n_ineligible == 0
        assert result.min_sessions == 3
        assert "insufficient" in result.detail.lower()

    def test_missing_decoupling_value_is_ineligible(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(21, 7200.0, 0.03, end=end),
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.05, end=end),
            _point(0, 7200.0, None, end=end),
        ]
        result = durability_trend(sessions)
        assert result.status == "trend"
        assert result.n_eligible == 3
        assert result.n_ineligible == 1
        assert result.ineligible[0].reason == "no_decoupling_value"

    def test_sessions_outside_window_are_ineligible(self) -> None:
        """The window anchors on the latest session and covers window_days;
        a long session older than that is ineligible with its reason."""
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(200, 7200.0, 0.02, end=end),  # far outside the 84-day window
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.05, end=end),
            _point(0, 7200.0, 0.06, end=end),
        ]
        result = durability_trend(sessions)
        assert result.status == "trend"
        assert result.n_eligible == 3
        assert result.n_ineligible == 1
        assert result.ineligible[0].reason == "outside_window"
        assert result.window_end == end
        assert result.window_start == end - dt.timedelta(days=83)

    def test_window_and_counts_are_reported(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.05, end=end),
            _point(0, 7200.0, 0.06, end=end),
        ]
        result = durability_trend(sessions)
        assert result.window_days == DEFAULT_TREND_WINDOW_DAYS
        assert result.min_long_session_seconds == DEFAULT_MIN_LONG_SESSION_SECONDS
        assert len(result.eligible) == 3
        assert result.eligible[0].session_date == end - dt.timedelta(days=14)
        assert result.eligible[-1].decoupling == pytest.approx(0.06)

    def test_minimum_session_count_is_configurable(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(7, 7200.0, 0.03, end=end),
            _point(0, 7200.0, 0.06, end=end),
        ]
        result = durability_trend(sessions, min_sessions=2)
        assert result.status == "trend"
        assert result.slope_per_day == pytest.approx(0.03 / 7.0)

    def test_long_threshold_is_configurable(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(14, 3600.0, 0.03, end=end),
            _point(7, 3600.0, 0.04, end=end),
            _point(0, 3600.0, 0.05, end=end),
        ]
        result = durability_trend(sessions, min_long_session_seconds=3600.0)
        assert result.n_eligible == 3

    def test_rejects_empty_session_list(self) -> None:
        with pytest.raises(ValueError, match="no sessions"):
            durability_trend([])

    def test_rejects_non_positive_duration(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [_point(0, 0.0, 0.03, end=end)]
        with pytest.raises(ValueError, match="duration"):
            durability_trend(sessions)

    def test_rejects_non_finite_decoupling(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [_point(0, 7200.0, float("nan"), end=end)]
        with pytest.raises(ValueError, match="finite"):
            durability_trend(sessions)

    def test_rejects_degenerate_same_date_fit(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(0, 7200.0, 0.03, end=end),
            _point(0, 7200.0, 0.04, end=end),
            _point(0, 7200.0, 0.05, end=end),
        ]
        with pytest.raises(ValueError, match="date"):
            durability_trend(sessions)

    def test_rejects_invalid_configuration(self) -> None:
        end = dt.date(2026, 10, 4)
        sessions = [
            _point(14, 7200.0, 0.04, end=end),
            _point(7, 7200.0, 0.05, end=end),
            _point(0, 7200.0, 0.06, end=end),
        ]
        with pytest.raises(ValueError, match="min_long_session_seconds"):
            durability_trend(sessions, min_long_session_seconds=0.0)
        with pytest.raises(ValueError, match="window_days"):
            durability_trend(sessions, window_days=0)
        with pytest.raises(ValueError, match="min_sessions"):
            durability_trend(sessions, min_sessions=1)
