"""Synthetic-data tests for the mean-maximal speed curve and the CS/D' fit.

ZON-5 (RED+GREEN) of ``odd/tasks/engine-zones.md`` (PROJECT_BRIEF section
7.3, section 12.4): every expected value is hand-derived from the linear
critical-speed model so the tests validate the implementation against
known parameters, not against itself.

Model (the running analogue of Monod & Scherrer 1965 / Jones et al. 2019;
PROJECT_BRIEF section 7.3):

    Distance(m) = CS(m/s) * t(s) + D'(m)          (linear 2-parameter form)
    mean speed over t: v(t) = Distance / t = CS + D' / t

The fit window is ROUGHLY 3-20 min (180-1200 s; section 7.3), and the fit
reuses the CP fit's OLS math (a shared private helper); only the axis
changes (work vs distance).

Reference curve used throughout (CS = 5.0 m/s, D' = 300 m). Hand
arithmetic for the noiseless points:

    t = 240 s  ->  Distance = 5.0*240 + 300 = 1500 m  ->  v = 1500/240  = 6.25 m/s
    t = 600 s  ->  Distance = 5.0*600 + 300 = 3300 m  ->  v = 3300/600  = 5.50 m/s
    t = 1200 s ->  Distance = 5.0*1200+ 300 = 6300 m  ->  v = 6300/1200 = 5.25 m/s

A least-squares fit of Distance against t on these exact points must
recover CS and D' to machine precision (tight tolerance 1e-9 relative),
and R^2 = 1 with RMSE = 0.

Step stream (300 s at 6.0 m/s then 300 s at 4.0 m/s) hand arithmetic:

    best 300 s = 6.0 m/s (the first block alone)
    best 600 s = (6.0*300 + 4.0*300) / 600 = 5.0 m/s

Real-data note: the owner has NO run threshold pace configured in
Intervals.icu, so there is no real run-speed stream to fit; per section
12.4 the acceptance for this feature is validation on synthetic data with
known parameters — exactly what this module does. The CS/D' output must
be treated accordingly until the owner configures a run threshold.

Noise tolerance rationale (``test_noisy_recovery_within_tolerance``): the
synthetic curve carries deterministic relative noise of at most 1% on
each point. Least squares over ten well-spread durations (180-1200 s)
damps independent relative errors roughly proportionally to their size,
so a priori 2% on CS and 10% on D' (the intercept is the noisier term)
is a comfortable bound; the test additionally requires R^2 > 0.98 and a
strictly positive reported RMSE.
"""

from dataclasses import FrozenInstanceError
from typing import Final

import pytest

from app.engine.zones import (
    CriticalSpeedFit,
    fit_critical_speed,
    mean_maximal_speed_curve,
)

CS: Final = 5.0
D_PRIME: Final = 300.0

# Durations inside the 3-20 min fit window, all with hand-derivable speeds.
FIT_DURATIONS: Final = (180, 240, 300, 360, 480, 600, 720, 900, 1080, 1200)

# Deterministic relative noise (multiplicative), max magnitude 1%.
NOISE: Final = (1.01, 0.99, 1.005, 0.995, 1.0, 0.992, 1.008, 0.985, 1.002, 0.998)


def synthetic_curve(
    durations: tuple[int, ...] = FIT_DURATIONS,
    noise: tuple[float, ...] | None = None,
) -> dict[int, float]:
    """Mean-maximal speed curve from the exact linear model: v(t) = CS + D'/t."""
    curve: dict[int, float] = {}
    for index, duration in enumerate(durations):
        speed = CS + D_PRIME / duration
        if noise is not None:
            speed *= noise[index]
        curve[duration] = speed
    return curve


class TestMeanMaximalSpeedCurve:
    def test_constant_speed_is_best_speed_for_every_duration(self) -> None:
        stream: list[float | None] = [4.0] * 600
        curve = mean_maximal_speed_curve(stream, durations_s=(1, 60, 120, 300, 480, 600))
        assert set(curve) == {1, 60, 120, 300, 480, 600}
        for duration in (1, 60, 120, 300, 480, 600):
            assert curve[duration] == pytest.approx(4.0)

    def test_durations_longer_than_stream_are_absent(self) -> None:
        stream: list[float | None] = [4.0] * 600
        curve = mean_maximal_speed_curve(stream)  # default duration set
        assert 1800 not in curve
        assert 3600 not in curve
        assert 600 in curve

    def test_step_stream_hand_computed(self) -> None:
        stream = [6.0] * 300 + [4.0] * 300
        curve = mean_maximal_speed_curve(stream, durations_s=(60, 300, 600))
        assert curve[60] == pytest.approx(6.0)
        assert curve[300] == pytest.approx(6.0)
        # (6.0*300 + 4.0*300) / 600 = 5.0 m/s (see module docstring).
        assert curve[600] == pytest.approx(5.0)

    def test_missing_sample_strict_default_disqualifies_gapped_windows(self) -> None:
        stream: list[float | None] = [4.0, 4.0, None, 4.0, 4.0]
        curve = mean_maximal_speed_curve(stream, durations_s=(2, 4))
        # Both 4-sample windows (0-3, 1-4) contain the gap at index 2 ->
        # no qualifying window -> duration 4 absent from the result.
        assert 4 not in curve
        # 2-sample windows 0-1 and 3-4 are complete -> best is exactly 4.0 m/s.
        assert curve[2] == pytest.approx(4.0)

    def test_missing_sample_relaxed_fraction_averages_valid_samples(self) -> None:
        stream: list[float | None] = [4.0, None, 6.0]
        # Strict: the only 3-sample window has 2/3 valid -> disqualified -> absent.
        strict = mean_maximal_speed_curve(stream, durations_s=(3,), min_valid_fraction=1.0)
        assert 3 not in strict
        # Relaxed: 2/3 >= 0.5 qualifies; the mean is over VALID samples only:
        # (4.0 + 6.0) / 2 = 5.0 m/s (gaps do not drag the mean toward zero).
        relaxed = mean_maximal_speed_curve(stream, durations_s=(3,), min_valid_fraction=0.5)
        assert relaxed[3] == pytest.approx(5.0)

    def test_empty_and_all_none_streams_raise(self) -> None:
        with pytest.raises(ValueError, match="no usable speed data"):
            mean_maximal_speed_curve([])
        with pytest.raises(ValueError, match="no usable speed data"):
            mean_maximal_speed_curve([None, None, None])

    def test_invalid_parameters_raise(self) -> None:
        stream: list[float | None] = [4.0] * 10
        with pytest.raises(ValueError, match="sample_interval"):
            mean_maximal_speed_curve(stream, sample_interval_s=0.0)
        with pytest.raises(ValueError, match="min_valid_fraction"):
            mean_maximal_speed_curve(stream, min_valid_fraction=1.5)
        with pytest.raises(ValueError, match="durations"):
            mean_maximal_speed_curve(stream, durations_s=())


class TestFitCriticalSpeed:
    def test_noiseless_recovery_is_exact(self) -> None:
        fit = fit_critical_speed(synthetic_curve())
        assert fit.cs_mps == pytest.approx(CS, rel=1e-9)
        assert fit.d_prime_meters == pytest.approx(D_PRIME, rel=1e-9)
        assert fit.r_squared == pytest.approx(1.0, abs=1e-12)
        assert fit.rmse_meters == pytest.approx(0.0, abs=1e-9)
        assert fit.n_points == len(FIT_DURATIONS)

    def test_noisy_recovery_within_tolerance(self) -> None:
        fit = fit_critical_speed(synthetic_curve(noise=NOISE))
        assert fit.cs_mps == pytest.approx(CS, rel=0.02)
        assert fit.d_prime_meters == pytest.approx(D_PRIME, rel=0.10)
        assert fit.r_squared > 0.98
        assert fit.rmse_meters > 0.0

    def test_hand_computed_two_point_fit(self) -> None:
        # OLS on two exact points is the straight line through them:
        # (240, 1500) and (600, 3300) -> slope CS = (3300-1500)/(600-240) = 5.0,
        # intercept D' = 1500 - 5.0*240 = 300 (see module docstring arithmetic).
        fit = fit_critical_speed({240: 6.25, 600: 5.5})
        assert fit.cs_mps == pytest.approx(CS, rel=1e-9)
        assert fit.d_prime_meters == pytest.approx(D_PRIME, rel=1e-9)
        assert fit.n_points == 2

    def test_window_bounds_are_enforced(self) -> None:
        # Points OUTSIDE the 3-20 min window (60 s, 120 s below; 1800 s above)
        # carry values wildly off the model. If they leaked into the fit,
        # CS/D' would not be recovered exactly and n_points would grow.
        curve = synthetic_curve()
        curve[60] = 100.0
        curve[120] = 0.001
        curve[1800] = 100.0
        fit = fit_critical_speed(curve)
        assert fit.cs_mps == pytest.approx(CS, rel=1e-9)
        assert fit.d_prime_meters == pytest.approx(D_PRIME, rel=1e-9)
        assert fit.n_points == len(FIT_DURATIONS)

    def test_window_bounds_are_configurable(self) -> None:
        fit = fit_critical_speed(synthetic_curve(), min_duration_s=300.0, max_duration_s=600.0)
        assert fit.n_points == 4  # 300, 360, 480, 600 only
        assert fit.cs_mps == pytest.approx(CS, rel=1e-9)
        assert fit.d_prime_meters == pytest.approx(D_PRIME, rel=1e-9)

    def test_too_few_points_raise(self) -> None:
        with pytest.raises(ValueError, match="not enough curve points"):
            fit_critical_speed({240: 6.25})

    def test_all_points_outside_window_raise(self) -> None:
        with pytest.raises(ValueError, match="not enough curve points"):
            fit_critical_speed({60: 6.0, 120: 5.5, 1800: 5.1})

    def test_empty_curve_raises(self) -> None:
        with pytest.raises(ValueError, match="not enough curve points"):
            fit_critical_speed({})

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError, match="fit window"):
            fit_critical_speed(synthetic_curve(), min_duration_s=600.0, max_duration_s=600.0)
        with pytest.raises(ValueError, match="fit window"):
            fit_critical_speed(synthetic_curve(), min_duration_s=-1.0, max_duration_s=1200.0)

    def test_result_is_frozen(self) -> None:
        fit = fit_critical_speed(synthetic_curve())
        with pytest.raises(FrozenInstanceError):
            fit.cs_mps = 99.0  # type: ignore[misc]

    def test_is_critical_speed_fit_instance(self) -> None:
        assert isinstance(fit_critical_speed(synthetic_curve()), CriticalSpeedFit)
