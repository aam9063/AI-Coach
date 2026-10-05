"""Reference, guard and fit-quality tests for the Banister impulse-response model.

Tests the pure module ``app/engine/banister.py`` (PROJECT_BRIEF section 7.2):

    p(t) = p0 + k1 * SUM w(s) e^(-(t-s)/tau1) - k2 * SUM w(s) e^(-(t-s)/tau2)

with defaults tau1 = 42 days (fitness) and tau2 = 7 days (fatigue).

Hand arithmetic for the deterministic two-session reference
(evaluate_banister, default time constants, p0 = 50, k1 = 0.05, k2 = 0.10):

    sessions: s1 = 2025-01-01 (load 100), s2 = 2025-01-03 (load 50)
    target:   t  = 2025-01-08, so t - s1 = 7 days and t - s2 = 5 days

    e^(-7/42) = e^(-1/6)   = 0.8464817249
    e^(-5/42)             = 0.8877655252
    e^(-7/7)  = e^(-1)     = 0.3678794412
    e^(-5/7)              = 0.4895416596

    fitness_sum = 100 * 0.8464817249 + 50 * 0.8877655252
                = 84.6481724900 + 44.3882762600 = 129.0364487494
    fatigue_sum = 100 * 0.3678794412 + 50 * 0.4895416596
                = 36.7879441200 + 24.4770829800 = 61.2650270950

    p(t) = 50 + 0.05 * 129.0364487494 - 0.10 * 61.2650270950
         = 50 + 6.4518224375 - 6.1265027095
         = 50.3253197280

(the assertion below recomputes this with exact ``math.exp`` arithmetic
instead of the rounded decimals).

Synthetic recovery case: a deterministic 70-day daily-load series generated
from known parameters (p0 = 50, k1 = 0.05, k2 = 0.10, tau1 = 42, tau2 = 7)
with 13 performance markers (every 4 days from day 16 to day 64) plus small
deterministic noise (sigma = 0.5 on a performance around 110, i.e. < 0.5%).
Documented tolerances: the fit must recover k1, k2 and p0 within 5% relative
of their true values (the noise level is < 0.5% of the performance, and a
13-marker fit leaves 10 residual degrees of freedom over the 3 free
parameters), and the reported R^2/RMSE must match an independently
computed R^2/RMSE (computed in this file from a local re-implementation of
the formula, NOT via the module under test) to within rel 1e-7.
"""

import datetime as dt
import math
import random
from typing import Final

import pytest

import app.engine.banister as banister_module
from app.engine.banister import (
    REASON_FIT_FAILED,
    REASON_FITTED,
    REASON_INSUFFICIENT_MARKERS,
    evaluate_banister,
    fit_banister,
)

S1: Final = dt.date(2025, 1, 1)
S2: Final = dt.date(2025, 1, 3)
TARGET: Final = dt.date(2025, 1, 8)

# True parameters used to generate the synthetic recovery case.
TRUE_P0: Final = 50.0
TRUE_K1: Final = 0.05
TRUE_K2: Final = 0.10
TRUE_TAU1: Final = 42.0
TRUE_TAU2: Final = 7.0

NOISE_SIGMA: Final = 0.5
RECOVERY_REL_TOL: Final = 0.05  # documented 5% parameter-recovery tolerance


def _reference_performance(
    loads: dict[dt.date, float],
    target: dt.date,
    p0: float,
    k1: float,
    k2: float,
    tau1: float,
    tau2: float,
) -> float:
    """Independent local re-implementation of the Banister formula (test oracle).

    Deliberately NOT calling the module under test, so module evaluation and
    fit quality can be cross-checked against this implementation.
    """
    fitness = math.fsum(
        w * math.exp(-((target - s).days) / tau1) for s, w in loads.items() if s <= target
    )
    fatigue = math.fsum(
        w * math.exp(-((target - s).days) / tau2) for s, w in loads.items() if s <= target
    )
    return p0 + k1 * fitness - k2 * fatigue


def _synthetic_case() -> tuple[dict[dt.date, float], dict[dt.date, float]]:
    """Deterministic 70-day load series + 13 noisy markers from known truth."""
    start = dt.date(2025, 1, 1)
    rng = random.Random(42)
    loads = {
        start + dt.timedelta(days=i): max(0.0, 60.0 + rng.gauss(0.0, 15.0))
        for i in range(70)
    }
    markers: dict[dt.date, float] = {}
    for i in range(16, 66, 4):
        day = start + dt.timedelta(days=i)
        truth = _reference_performance(
            loads, day, TRUE_P0, TRUE_K1, TRUE_K2, TRUE_TAU1, TRUE_TAU2
        )
        markers[day] = truth + rng.gauss(0.0, NOISE_SIGMA)
    return loads, markers


class TestEvaluateBanister:
    def test_hand_derived_reference_two_sessions(self) -> None:
        """2 sessions, hand-derived expectation (see module docstring)."""
        loads = {S1: 100.0, S2: 50.0}
        result = evaluate_banister(
            loads, target_date=TARGET, p0=50.0, k1=0.05, k2=0.10
        )
        expected = 50.0 + 0.05 * (
            100.0 * math.exp(-7.0 / 42.0) + 50.0 * math.exp(-5.0 / 42.0)
        ) - 0.10 * (100.0 * math.exp(-1.0) + 50.0 * math.exp(-5.0 / 7.0))
        assert result == pytest.approx(expected, rel=1e-9)
        assert result == pytest.approx(50.3253197280, rel=1e-9)

    def test_matches_independent_implementation_on_synthetic_case(self) -> None:
        loads, markers = _synthetic_case()
        for day in markers:
            expected = _reference_performance(
                loads, day, TRUE_P0, TRUE_K1, TRUE_K2, TRUE_TAU1, TRUE_TAU2
            )
            assert evaluate_banister(
                loads, target_date=day, p0=TRUE_P0, k1=TRUE_K1, k2=TRUE_K2
            ) == pytest.approx(expected, rel=1e-12)

    def test_time_constants_are_overridable(self) -> None:
        # tau1 = 14, tau2 = 3: same two sessions, t - s1 = 7, t - s2 = 5.
        loads = {S1: 100.0, S2: 50.0}
        result = evaluate_banister(
            loads,
            target_date=TARGET,
            p0=10.0,
            k1=0.20,
            k2=0.05,
            tau1_days=14.0,
            tau2_days=3.0,
        )
        expected = 10.0 + 0.20 * (
            100.0 * math.exp(-0.5) + 50.0 * math.exp(-5.0 / 14.0)
        ) - 0.05 * (100.0 * math.exp(-7.0 / 3.0) + 50.0 * math.exp(-5.0 / 3.0))
        assert result == pytest.approx(expected, rel=1e-12)

    def test_same_day_session_contributes_full_weight(self) -> None:
        # A session ON the target date has t - s = 0, so e^0 = 1 (full weight).
        loads = {TARGET: 10.0}
        result = evaluate_banister(
            loads, target_date=TARGET, p0=40.0, k1=0.5, k2=0.2
        )
        assert result == pytest.approx(40.0 + 0.5 * 10.0 - 0.2 * 10.0, rel=1e-12)

    def test_sessions_after_target_are_causally_excluded(self) -> None:
        # A session AFTER t must not contribute (non-causal leakage forbidden).
        loads = {S1: 100.0, TARGET + dt.timedelta(days=1): 100.0}
        result = evaluate_banister(
            loads, target_date=TARGET, p0=50.0, k1=0.05, k2=0.10
        )
        only_past = evaluate_banister(
            {S1: 100.0}, target_date=TARGET, p0=50.0, k1=0.05, k2=0.10
        )
        assert result == pytest.approx(only_past, rel=1e-12)

    def test_rejects_empty_load_series(self) -> None:
        # An empty mapping cannot be distinguished from missing data: reject.
        with pytest.raises(ValueError, match="empty"):
            evaluate_banister({}, target_date=TARGET, p0=50.0, k1=0.05, k2=0.10)

    def test_rejects_negative_load(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            evaluate_banister(
                {S1: -1.0}, target_date=TARGET, p0=50.0, k1=0.05, k2=0.10
            )

    def test_rejects_non_positive_time_constant(self) -> None:
        loads = {S1: 100.0}
        for tau in (0.0, -7.0):
            with pytest.raises(ValueError, match="tau"):
                evaluate_banister(
                    loads,
                    target_date=TARGET,
                    p0=50.0,
                    k1=0.05,
                    k2=0.10,
                    tau1_days=tau,
                )
            with pytest.raises(ValueError, match="tau"):
                evaluate_banister(
                    loads,
                    target_date=TARGET,
                    p0=50.0,
                    k1=0.05,
                    k2=0.10,
                    tau2_days=tau,
                )

    def test_rejects_negative_gains(self) -> None:
        # k1/k2 are positive gains in the Banister formulation: reject negatives.
        loads = {S1: 100.0}
        with pytest.raises(ValueError, match="k1"):
            evaluate_banister(loads, target_date=TARGET, p0=50.0, k1=-0.1, k2=0.1)
        with pytest.raises(ValueError, match="k2"):
            evaluate_banister(loads, target_date=TARGET, p0=50.0, k1=0.1, k2=-0.1)

    def test_rejects_non_finite_parameters(self) -> None:
        loads = {S1: 100.0}
        with pytest.raises(ValueError, match="finite"):
            evaluate_banister(
                loads, target_date=TARGET, p0=float("nan"), k1=0.05, k2=0.10
            )
        with pytest.raises(ValueError, match="finite"):
            evaluate_banister(
                loads, target_date=TARGET, p0=50.0, k1=float("inf"), k2=0.10
            )


class TestFitGuard:
    """The core requirement: an unfitted model can never look personalized."""

    def test_no_markers_owner_reality(self) -> None:
        """The owner has NO performance markers at all today (no race results,
        no test efforts): the empty-marker path is the path that will actually
        run, so it must be the well-tested, safe default."""
        loads, _ = _synthetic_case()
        fit = fit_banister(loads, {})
        assert fit.personalized is False
        assert fit.reason == REASON_INSUFFICIENT_MARKERS
        assert fit.parameters is None
        assert fit.quality is None

    def test_too_few_markers_is_rejected_not_fitted(self) -> None:
        loads, markers = _synthetic_case()
        few = dict(list(markers.items())[:9])
        assert len(few) == 9
        fit = fit_banister(loads, few)
        assert fit.personalized is False
        assert fit.reason == REASON_INSUFFICIENT_MARKERS
        assert fit.parameters is None
        assert fit.quality is None

    def test_exactly_min_markers_is_fitted(self) -> None:
        loads, markers = _synthetic_case()
        exactly = dict(list(markers.items())[:10])
        assert len(exactly) == 10
        fit = fit_banister(loads, exactly)
        assert fit.personalized is True
        assert fit.reason == REASON_FITTED
        assert fit.parameters is not None
        assert fit.quality is not None
        assert fit.quality.marker_count == 10

    def test_min_markers_is_configurable(self) -> None:
        loads, markers = _synthetic_case()
        few = dict(list(markers.items())[:4])
        fit = fit_banister(loads, few, min_markers=4)
        assert fit.personalized is True
        assert fit.reason == REASON_FITTED

    def test_reason_strings_are_machine_readable(self) -> None:
        # The guard must be machine-readable: exact constant strings, no prose.
        assert REASON_FITTED == "fitted"
        assert REASON_INSUFFICIENT_MARKERS == "insufficient_markers"
        assert REASON_FIT_FAILED == "fit_failed"

    def test_fit_failure_is_never_presented_as_personalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed optimizer run must land on the unfitted side of the guard."""

        class _FailedResult:
            success = False
            message = "maximum function evaluations exceeded"

        monkeypatch.setattr(
            banister_module, "least_squares", lambda *args, **kwargs: _FailedResult()
        )
        loads, markers = _synthetic_case()
        fit = fit_banister(loads, markers)
        assert fit.personalized is False
        assert fit.reason == REASON_FIT_FAILED
        assert fit.parameters is None
        assert fit.quality is None


class TestFitValidation:
    def test_rejects_empty_load_series(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            fit_banister({}, {TARGET: 100.0})

    def test_rejects_non_positive_min_markers(self) -> None:
        loads, markers = _synthetic_case()
        for bad in (0, -3):
            with pytest.raises(ValueError, match="min_markers"):
                fit_banister(loads, markers, min_markers=bad)

    def test_rejects_non_positive_time_constant(self) -> None:
        loads, markers = _synthetic_case()
        with pytest.raises(ValueError, match="tau"):
            fit_banister(loads, markers, tau1_days=0.0)
        with pytest.raises(ValueError, match="tau"):
            fit_banister(loads, markers, tau2_days=-1.0)

    def test_rejects_non_finite_marker_value(self) -> None:
        loads, _ = _synthetic_case()
        with pytest.raises(ValueError, match="finite"):
            fit_banister(loads, {TARGET: float("nan")})
        with pytest.raises(ValueError, match="finite"):
            fit_banister(loads, {TARGET: float("inf")})

    def test_rejects_zero_variance_markers(self) -> None:
        # Identical markers: R^2 is undefined (SS_tot = 0). Reject, never degrade.
        loads, _ = _synthetic_case()
        markers = {
            dt.date(2025, 1, 1) + dt.timedelta(days=i * 4): 100.0 for i in range(10)
        }
        with pytest.raises(ValueError, match="zero variance"):
            fit_banister(loads, markers)


class TestFitQuality:
    def test_synthetic_recovery_and_quality(self) -> None:
        """Fit on synthetic data from known k1/k2: parameters are recovered
        within the documented 5% tolerance, and the reported R^2/RMSE match an
        independently computed R^2/RMSE (via the local oracle implementation)."""
        loads, markers = _synthetic_case()
        fit = fit_banister(loads, markers)
        assert fit.personalized is True
        assert fit.reason == REASON_FITTED
        params = fit.parameters
        quality = fit.quality
        assert params is not None
        assert quality is not None

        # Parameter recovery within the documented tolerance.
        assert params.p0 == pytest.approx(TRUE_P0, rel=RECOVERY_REL_TOL)
        assert params.k1 == pytest.approx(TRUE_K1, rel=RECOVERY_REL_TOL)
        assert params.k2 == pytest.approx(TRUE_K2, rel=RECOVERY_REL_TOL)
        # Time constants stay fixed unless explicitly fitted.
        assert params.tau1_days == TRUE_TAU1
        assert params.tau2_days == TRUE_TAU2

        # Independent R^2 / RMSE from the local oracle, not the module.
        predicted = [
            _reference_performance(
                loads,
                day,
                params.p0,
                params.k1,
                params.k2,
                params.tau1_days,
                params.tau2_days,
            )
            for day in markers
        ]
        actual = [markers[day] for day in markers]
        residuals = [p - a for p, a in zip(predicted, actual, strict=True)]
        ss_res = math.fsum(r * r for r in residuals)
        mean_actual = math.fsum(actual) / len(actual)
        ss_tot = math.fsum((a - mean_actual) ** 2 for a in actual)
        expected_r2 = 1.0 - ss_res / ss_tot
        expected_rmse = math.sqrt(ss_res / len(actual))

        assert quality.marker_count == len(markers) == 13
        assert quality.r2 == pytest.approx(expected_r2, rel=1e-7)
        assert quality.rmse == pytest.approx(expected_rmse, rel=1e-7)
        # Fit quality on < 0.5% noise must be near-perfect.
        assert quality.r2 >= 0.98
        assert quality.rmse == pytest.approx(NOISE_SIGMA, rel=0.5)

    def test_time_constants_can_be_fitted(self) -> None:
        """fit_time_constants=True fits tau1/tau2 as well (documented loose
        tolerance: time constants are ill-conditioned in Banister fits, so the
        assertion is a bracket around the truth, not a tight recovery)."""
        loads, markers = _synthetic_case()
        fit = fit_banister(loads, markers, fit_time_constants=True)
        assert fit.personalized is True
        assert fit.reason == REASON_FITTED
        params = fit.parameters
        assert params is not None
        assert params.tau1_days == pytest.approx(TRUE_TAU1, abs=8.0)
        assert params.tau2_days == pytest.approx(TRUE_TAU2, abs=3.0)
        assert fit.quality is not None
        assert fit.quality.marker_count == len(markers)
