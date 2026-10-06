"""Synthetic-data tests for the mean-maximal power curve and the CP/W' fit.

ZON-1 (RED) of ``odd/tasks/engine-zones.md`` (PROJECT_BRIEF section 7.3,
section 12.4): every expected value is hand-derived from the linear
critical-power model so the tests validate the implementation against
known parameters, not against itself.

Model (Monod & Scherrer 1965; Jones et al. 2019):

    Work(J) = CP(W) * t(s) + W'(J)          (linear 2-parameter form)
    mean power over t: P(t) = Work / t = CP + W' / t

Reference curve used throughout (CP = 250 W, W' = 18000 J). Hand
arithmetic for the noiseless points:

    t = 180 s  ->  Work = 250*180  + 18000 =  63000 J  ->  P = 63000/180   = 350.0 W
    t = 300 s  ->  Work = 250*300  + 18000 =  93000 J  ->  P = 93000/300   = 310.0 W
    t = 1200 s ->  Work = 250*1200 + 18000 = 318000 J  ->  P = 318000/1200 = 265.0 W

A least-squares fit of Work against t on these exact points must recover
CP and W' to machine precision (tight tolerance 1e-9 relative), and R^2
= 1 with RMSE = 0.

Step stream (300 s at 250 W then 300 s at 150 W) hand arithmetic:

    best 300 s = 250.0 W (the first block alone)
    best 600 s = (250*300 + 150*300) / 600 = (75000 + 45000) / 600 = 200.0 W

Real-data note: the owner has NO power meter, so there is no real power
stream to fit; per section 12.4 the acceptance for this feature is
validation on synthetic data with known parameters — exactly what this
module does.

Noise tolerance rationale (``test_noisy_recovery_within_tolerance``): the
synthetic curve carries deterministic relative noise of at most 1% on
each point. Least squares over ten well-spread durations (180-1200 s)
damps independent relative errors roughly proportionally to their size,
so a priori 2% on CP and 10% on W' (the intercept is the noisier term)
is a comfortable bound; the test additionally requires R^2 > 0.98 and a
strictly positive reported RMSE.
"""

from dataclasses import FrozenInstanceError
from typing import Final

import pytest

from app.engine.zones import (
    CriticalPowerFit,
    fit_critical_power,
    mean_maximal_power_curve,
)

CP: Final = 250.0
W_PRIME: Final = 18000.0

# Durations inside the 2-20 min fit window, all with hand-derivable powers.
FIT_DURATIONS: Final = (180, 240, 300, 360, 480, 600, 720, 900, 1080, 1200)

# Deterministic relative noise (multiplicative), max magnitude 1%.
NOISE: Final = (1.01, 0.99, 1.005, 0.995, 1.0, 0.992, 1.008, 0.985, 1.002, 0.998)


def synthetic_curve(
    durations: tuple[int, ...] = FIT_DURATIONS,
    noise: tuple[float, ...] | None = None,
) -> dict[int, float]:
    """Mean-maximal power curve from the exact linear model: P(t) = CP + W'/t."""
    curve: dict[int, float] = {}
    for index, duration in enumerate(durations):
        power = CP + W_PRIME / duration
        if noise is not None:
            power *= noise[index]
        curve[duration] = power
    return curve


class TestMeanMaximalPowerCurve:
    def test_constant_power_is_best_power_for_every_duration(self) -> None:
        stream: list[float | None] = [200.0] * 600
        curve = mean_maximal_power_curve(stream, durations_s=(1, 60, 120, 300, 480, 600))
        assert set(curve) == {1, 60, 120, 300, 480, 600}
        for duration in (1, 60, 120, 300, 480, 600):
            assert curve[duration] == pytest.approx(200.0)

    def test_durations_longer_than_stream_are_absent(self) -> None:
        stream: list[float | None] = [200.0] * 600
        curve = mean_maximal_power_curve(stream)  # default duration set
        assert 1800 not in curve
        assert 3600 not in curve
        assert 600 in curve

    def test_step_stream_hand_computed(self) -> None:
        stream = [250.0] * 300 + [150.0] * 300
        curve = mean_maximal_power_curve(stream, durations_s=(60, 300, 600))
        assert curve[60] == pytest.approx(250.0)
        assert curve[300] == pytest.approx(250.0)
        # (250*300 + 150*300) / 600 = 200.0 W (see module docstring).
        assert curve[600] == pytest.approx(200.0)

    def test_missing_sample_strict_default_disqualifies_gapped_windows(self) -> None:
        stream: list[float | None] = [200.0, 200.0, None, 200.0, 200.0]
        curve = mean_maximal_power_curve(stream, durations_s=(2, 4))
        # Both 4-sample windows (0-3, 1-4) contain the gap at index 2 ->
        # no qualifying window -> duration 4 absent from the result.
        assert 4 not in curve
        # 2-sample windows 0-1 and 3-4 are complete -> best is exactly 200 W.
        assert curve[2] == pytest.approx(200.0)

    def test_missing_sample_relaxed_fraction_averages_valid_samples(self) -> None:
        stream: list[float | None] = [200.0, None, 400.0]
        # Strict: the only 3-sample window has 2/3 valid -> disqualified -> absent.
        strict = mean_maximal_power_curve(stream, durations_s=(3,), min_valid_fraction=1.0)
        assert 3 not in strict
        # Relaxed: 2/3 >= 0.5 qualifies; the mean is over VALID samples only:
        # (200 + 400) / 2 = 300.0 W (gaps do not drag the mean toward zero).
        relaxed = mean_maximal_power_curve(stream, durations_s=(3,), min_valid_fraction=0.5)
        assert relaxed[3] == pytest.approx(300.0)

    def test_empty_and_all_none_streams_raise(self) -> None:
        with pytest.raises(ValueError, match="no usable power data"):
            mean_maximal_power_curve([], durations_s=(60,))
        with pytest.raises(ValueError, match="no usable power data"):
            mean_maximal_power_curve([None, None, None], durations_s=(60,))

    def test_sample_interval_scales_window_length(self) -> None:
        stream: list[float | None] = [200.0] * 10  # 0.5 Hz stream: 10 samples = 20 s
        curve = mean_maximal_power_curve(
            stream, sample_interval_s=2.0, durations_s=(10,)
        )
        assert curve[10] == pytest.approx(200.0)  # window of 5 samples
        # 1 s at 0.5 Hz is half a sample: no qualifying window -> absent.
        sub_sample = mean_maximal_power_curve(
            stream, sample_interval_s=2.0, durations_s=(1,)
        )
        assert 1 not in sub_sample

    def test_rejects_bad_parameters(self) -> None:
        stream: list[float | None] = [200.0] * 10
        with pytest.raises(ValueError, match="sample_interval"):
            mean_maximal_power_curve(stream, sample_interval_s=0.0)
        with pytest.raises(ValueError, match="min_valid_fraction"):
            mean_maximal_power_curve(stream, min_valid_fraction=0.0)
        with pytest.raises(ValueError, match="min_valid_fraction"):
            mean_maximal_power_curve(stream, min_valid_fraction=1.5)
        with pytest.raises(ValueError, match="positive"):
            mean_maximal_power_curve(stream, durations_s=(60, 0))
        with pytest.raises(ValueError, match="empty"):
            mean_maximal_power_curve(stream, durations_s=())


class TestFitCriticalPower:
    def test_noiseless_recovery_is_exact(self) -> None:
        fit = fit_critical_power(synthetic_curve())
        assert fit.cp_watts == pytest.approx(CP, rel=1e-9)
        assert fit.w_prime_joules == pytest.approx(W_PRIME, rel=1e-9)
        assert fit.r_squared == pytest.approx(1.0, abs=1e-12)
        assert fit.rmse_joules < 1e-6
        assert fit.n_points == len(FIT_DURATIONS)

    def test_returns_typed_frozen_result(self) -> None:
        fit = fit_critical_power(synthetic_curve())
        assert isinstance(fit, CriticalPowerFit)
        with pytest.raises(FrozenInstanceError):
            fit.cp_watts = 999.0  # type: ignore[misc]

    def test_durations_outside_window_are_ignored(self) -> None:
        base = fit_critical_power(synthetic_curve())
        curve = dict(synthetic_curve())
        # Out-of-window points generated by a DIFFERENT model would skew the
        # fit if they were used; the fit must ignore them entirely.
        curve[60] = CP + W_PRIME / 60 + 200.0  # t = 1 min  (< 2 min window)
        curve[3600] = 100.0  # t = 60 min (> 20 min window)
        extended = fit_critical_power(curve)
        assert extended == base

    def test_fewer_points_than_needed_raises(self) -> None:
        with pytest.raises(ValueError, match="at least two"):
            fit_critical_power({300: 310.0})
        with pytest.raises(ValueError, match="at least two"):
            fit_critical_power({})

    def test_rejects_inverted_window(self) -> None:
        with pytest.raises(ValueError, match="window"):
            fit_critical_power(
                synthetic_curve(), min_duration_s=1200.0, max_duration_s=120.0
            )

    def test_noisy_recovery_within_tolerance(self) -> None:
        fit = fit_critical_power(synthetic_curve(noise=NOISE))
        # See module docstring for the tolerance rationale.
        assert fit.cp_watts == pytest.approx(CP, rel=0.02)
        assert fit.w_prime_joules == pytest.approx(W_PRIME, rel=0.10)
        assert fit.r_squared > 0.98
        assert fit.rmse_joules > 0.0
        assert fit.n_points == len(FIT_DURATIONS)

    def test_window_bounds_are_respected(self) -> None:
        # Raising the window floor to 4 min drops the 180 s and 300 s points.
        fit = fit_critical_power(synthetic_curve(), min_duration_s=240.0)
        assert fit.n_points == len(FIT_DURATIONS) - 1
