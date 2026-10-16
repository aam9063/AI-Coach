"""Hand-computed reference tests for the race prediction engine.

RP-1..RP-9 of ``odd/tasks/race-prediction.md`` (PROJECT_BRIEF section 7.7,
acceptance section 12.8): run predictions via Riegel and VDOT, the Martin et
al. 1998 bike physics solver validated against a hand-solved flat-course
example, swim pace from CSS, the run-off-the-bike slowdown factor, T1/T2
transitions, the Monte Carlo composite total and the prediction envelope
contract.

Every prediction is verified against hand-computed arithmetic or published
reference values, and every prediction output must satisfy the envelope
contract: exactly ``{point, lower, upper, method, assumptions,
model_version}`` with ``lower <= point <= upper`` — never a bare number.

Run references (RP-1)
---------------------
Riegel 1981 ("Athletic Records and Human Endurance", Exponential Law):

    T2 = T1 * (D2/D1)^1.06

    Published worked anchor: the doubling factor 2^1.06 = 2.08493... — Riegel's
own table for doubling the distance (5000 m in 20:00 -> 10000 m in
20:00 * 2^1.06 = 2501.92 s = 41:42).

VDOT equivalent: VDOT of the reference effort (Daniels & Gilbert, verbatim
formulas reused from ``app.engine.zones`` — Feature 4 — never reimplemented),
then the target time is the duration whose ``vdot_from_effort`` equals that
VDOT (numerical inversion; ``percent_vo2max`` depends on the target duration,
so the inversion is a bisection, self-consistency asserted).

    5000 m in 20:00 -> VDOT = 49.806 (ZON-6 anchor); the 10000 m time with
    the same VDOT is ~2488 s (41:28) — close to, but not identical to, the
    Riegel 2501.92 s, and the spread between both methods is part of the
    result (RP-2).
"""

import inspect
import math
from dataclasses import fields
from typing import Any

import numpy as np
import pytest

from app.engine import predict
from app.engine.predict import (
    DEFAULT_DRIVETRAIN_EFFICIENCY,
    GRAVITY_M_S2,
    PREDICTION_MODEL_VERSION,
    RIEGEL_EXPONENT,
    BikeCoursePrediction,
    RunPrediction,
    air_density,
    bike_course_prediction,
    bike_speed_for_power,
    default_bike_power_fraction,
    flat_course_speed_analytic,
    required_bike_power,
    riegel_time,
    vdot_equivalent_time,
)
from app.engine.zones import vdot_from_effort


class TestRiegelTime:
    def test_published_doubling_anchor_5k_20min_to_10k(self) -> None:
        # T2 = 1200 * (10000/5000)^1.06 = 1200 * 2^1.06 = 1200 * 2.084931
        #   = 2501.92 s = 41:42 — Riegel's published doubling factor.
        assert riegel_time(5000.0, 1200.0, 10000.0) == pytest.approx(2501.92, abs=0.01)

    def test_marathon_from_10k_hand_computed(self) -> None:
        # 10000 m in 40:00 -> marathon: 2400 * (42195/10000)^1.06
        #   = 2400 * 4.60017 = 11040.4 s = 3:04:00 (Riegel's classic example
        # family; computed here from the formula, pinned to 0.1 s).
        expected = 2400.0 * (42195.0 / 10000.0) ** 1.06
        assert riegel_time(10000.0, 2400.0, 42195.0) == pytest.approx(
            expected, abs=0.1
        )
        assert riegel_time(10000.0, 2400.0, 42195.0) == pytest.approx(11040.4, abs=1.0)

    def test_same_distance_returns_reference_time(self) -> None:
        # D2 = D1: (D2/D1)^1.06 = 1 -> identity, exactly the reference time.
        assert riegel_time(5000.0, 1234.0, 5000.0) == pytest.approx(1234.0)

    def test_custom_exponent_is_a_parameter(self) -> None:
        # The exponent is a documented, configurable module constant
        # (RIEGEL_EXPONENT = 1.06, LITERATURE) and a function parameter.
        assert pytest.approx(1.06) == RIEGEL_EXPONENT
        custom = riegel_time(5000.0, 1200.0, 10000.0, exponent=1.08)
        assert custom == pytest.approx(1200.0 * 2.0**1.08, rel=1e-12)
        assert custom > riegel_time(5000.0, 1200.0, 10000.0)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            riegel_time(0.0, 1200.0, 10000.0)
        with pytest.raises(ValueError, match="duration"):
            riegel_time(5000.0, 0.0, 10000.0)
        with pytest.raises(ValueError, match="distance"):
            riegel_time(5000.0, 1200.0, -10000.0)
        with pytest.raises(ValueError, match="exponent"):
            riegel_time(5000.0, 1200.0, 10000.0, exponent=0.0)


class TestVdotEquivalentTime:
    def test_inversion_is_self_consistent_5k_20min_to_10k(self) -> None:
        # VDOT(5000 m, 20:00) = 49.806 (ZON-6 anchor). The 10000 m time with
        # the same VDOT must reproduce that VDOT exactly when fed back into
        # the Feature 4 function (reuse, never reimplement).
        vdot_reference = vdot_from_effort(5000.0, 1200.0)
        assert vdot_reference == pytest.approx(49.806, abs=5e-3)
        t2 = vdot_equivalent_time(5000.0, 1200.0, 10000.0)
        assert vdot_from_effort(10000.0, t2) == pytest.approx(
            vdot_reference, rel=1e-6
        )
        # Hand-checked magnitude: ~41:28 (2488 s), between the 40:00
        # same-velocity effort and Riegel's 41:42.
        assert t2 == pytest.approx(2488.0, abs=30.0)

    def test_longer_target_is_slower_and_close_to_riegel(self) -> None:
        t2 = vdot_equivalent_time(5000.0, 1200.0, 10000.0)
        assert t2 > 1200.0
        # Both models describe the same physiology: within 2% of Riegel.
        assert t2 == pytest.approx(riegel_time(5000.0, 1200.0, 10000.0), rel=0.02)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            vdot_equivalent_time(0.0, 1200.0, 10000.0)
        with pytest.raises(ValueError, match="duration"):
            vdot_equivalent_time(5000.0, 0.0, 10000.0)
        with pytest.raises(ValueError, match="distance"):
            vdot_equivalent_time(5000.0, 1200.0, 0.0)


class TestPredictRun:
    def test_returns_both_methods_with_spread(self) -> None:
        result = predict.predict_run(5000.0, 1200.0, 10000.0)
        assert isinstance(result, RunPrediction)
        # Both methods present, each as a full envelope, and the spread
        # between them is reported (RP-2).
        assert result.riegel.point == pytest.approx(2501.92, abs=0.01)
        assert result.vdot.point == pytest.approx(
            vdot_equivalent_time(5000.0, 1200.0, 10000.0), rel=1e-9
        )
        assert result.spread_seconds == pytest.approx(
            abs(result.vdot.point - result.riegel.point), rel=1e-9
        )
        assert result.spread_seconds > 0.0

    def test_riegel_band_from_documented_exponent_range(self) -> None:
        # The Riegel band comes from the documented exponent uncertainty
        # (exponent +/- RIEGEL_EXPONENT_UNCERTAINTY): for a longer target the
        # larger exponent is slower, so lower < point < upper strictly.
        result = predict.predict_run(5000.0, 1200.0, 10000.0)
        exponent = RIEGEL_EXPONENT
        low = 1200.0 * 2.0 ** (exponent - predict.RIEGEL_EXPONENT_UNCERTAINTY)
        high = 1200.0 * 2.0 ** (exponent + predict.RIEGEL_EXPONENT_UNCERTAINTY)
        assert result.riegel.lower == pytest.approx(low, rel=1e-9)
        assert result.riegel.upper == pytest.approx(high, rel=1e-9)
        assert result.riegel.lower < result.riegel.point < result.riegel.upper

    def test_vdot_band_contains_point(self) -> None:
        result = predict.predict_run(5000.0, 1200.0, 10000.0)
        assert result.vdot.lower < result.vdot.point < result.vdot.upper

    def test_shorter_target_runs_band_the_other_way(self) -> None:
        # For a SHORTER target the exponent asymmetry flips: the larger
        # exponent is FASTER, so the lower bound must use the upper exponent.
        result = predict.predict_run(10000.0, 2400.0, 5000.0)
        low = 2400.0 * 0.5 ** (predict.RIEGEL_EXPONENT + 0.02)
        high = 2400.0 * 0.5 ** (predict.RIEGEL_EXPONENT - 0.02)
        assert result.riegel.lower == pytest.approx(low, rel=1e-9)
        assert result.riegel.upper == pytest.approx(high, rel=1e-9)
        assert result.riegel.lower < result.riegel.point < result.riegel.upper

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            predict.predict_run(0.0, 1200.0, 10000.0)
        with pytest.raises(ValueError, match="duration"):
            predict.predict_run(5000.0, -1200.0, 10000.0)


# ---------------------------------------------------------------------------
# Bike physics (RP-3/RP-4): Martin et al. 1998
#
# Hand-solved FLAT-course analytic example (RP-3, §12.8). The flat power
# equation 0.5*rho*CdA*v^3 + Crr*m*g*v - eta*P = 0 is a depressed CUBIC in v
# (a pure quadratic with v = sqrt(...) only arises for a given drive FORCE,
# not power), so the analytic example below is its exact Cardano solution:
#
#   A v^3 + B v - C = 0   with   A = 0.5*rho*CdA,  B = Crr*m*g,  C = eta*P
#   p = B/A,  q = -C/A,  D = (q/2)^2 + (p/3)^3  (D > 0 here, one real root)
#   v = cbrt(-q/2 + sqrt(D)) + cbrt(-q/2 - sqrt(D))
#
# Reference numbers (all fixed inputs, rho = 1.225 kg/m^3 = the published
# ISA sea-level 15 °C density):
#   A = 0.5 * 1.225 * 0.30 = 0.18375
#   B = 0.004 * 80 * 9.80665 = 3.138128
#   C = 0.976 * 171 = 166.896
#   p = 17.08042...,  q = -908.3265...
#   D = 454.1633^2 + 5.69347^3 = 206434.5 ...
#   v = cbrt(908.36) + cbrt(0.009) ~= 9.110 m/s (~32.8 km/h)
# ---------------------------------------------------------------------------

_FLAT_RHO = 1.225  # kg/m^3, published ISA sea-level density at 15 °C
_FLAT_CDA = 0.30  # m^2
_FLAT_CRR = 0.004  # dimensionless
_FLAT_MASS = 80.0  # kg
_FLAT_POWER = 171.0  # W (0.95 * 180 W FTP: sprint fraction, RP-4)


def _cardano_flat_speed(rho: float, cda: float, crr: float, mass: float, power: float) -> float:
    """Exact Cardano root of A v^3 + B v - eta*P = 0 (the hand solution)."""
    big_a = 0.5 * rho * cda
    big_b = crr * mass * GRAVITY_M_S2
    big_c = power * DEFAULT_DRIVETRAIN_EFFICIENCY
    p = big_b / big_a
    q = -big_c / big_a
    disc = (q / 2.0) ** 2 + (p / 3.0) ** 3

    def cbrt(x: float) -> float:
        return math.copysign(abs(x) ** (1.0 / 3.0), x)

    return cbrt(-q / 2.0 + math.sqrt(disc)) + cbrt(-q / 2.0 - math.sqrt(disc))


class TestAirDensity:
    def test_isa_sea_level_15c_is_1_225(self) -> None:
        # LITERATURE anchor: the International Standard Atmosphere gives
        # rho = 1.225 kg/m^3 at sea level and 15 °C exactly.
        assert air_density(15.0, 0.0) == pytest.approx(1.225, abs=5e-4)

    def test_temperature_and_altitude_lower_density(self) -> None:
        hot_low = air_density(35.0, 0.0)
        high = air_density(15.0, 2000.0)
        assert air_density(15.0, 0.0) > hot_low
        assert air_density(15.0, 0.0) > high
        assert air_density(25.0, 1500.0) == pytest.approx(0.9881, abs=5e-3)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="temperature"):
            air_density(-274.0, 0.0)
        with pytest.raises(ValueError, match="altitude"):
            air_density(15.0, -10.0)
        with pytest.raises(ValueError, match="altitude"):
            air_density(15.0, 11001.0)  # above the troposphere validity


class TestRequiredBikePower:
    def test_flat_hand_computed(self) -> None:
        # P = (0.5*rho*CdA*v^2 + Crr*m*g) * v / eta at v = 9.0 m/s, flat:
        #   (0.18375*81 + 3.138128) * 9 / 0.976 = 166.225 W (from the formula).
        expected = (
            0.5 * _FLAT_RHO * _FLAT_CDA * 9.0**2
            + _FLAT_CRR * _FLAT_MASS * GRAVITY_M_S2
        ) * 9.0 / DEFAULT_DRIVETRAIN_EFFICIENCY
        power = required_bike_power(
            9.0,
            gradient=0.0,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            air_density=_FLAT_RHO,
        )
        assert power == pytest.approx(expected, rel=1e-12)
        assert power == pytest.approx(166.225, abs=0.05)

    def test_climb_costs_more_than_descent_at_same_speed(self) -> None:
        kw = dict(cda=_FLAT_CDA, crr=_FLAT_CRR, mass_kg=_FLAT_MASS, air_density=_FLAT_RHO)
        flat = required_bike_power(9.0, gradient=0.0, **kw)
        up = required_bike_power(9.0, gradient=0.05, **kw)
        down = required_bike_power(9.0, gradient=-0.05, **kw)
        assert up > flat > down

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="speed"):
            required_bike_power(
                -1.0, gradient=0.0, cda=0.3, crr=0.004, mass_kg=80.0, air_density=1.225
            )
        with pytest.raises(ValueError, match="gradient"):
            required_bike_power(
                9.0, gradient=-1.5, cda=0.3, crr=0.004, mass_kg=80.0, air_density=1.225
            )


class TestFlatCourseAnalytic:
    def test_hand_solved_cardano_value(self) -> None:
        # The hand solution above: v ~= 9.110 m/s (32.8 km/h) at 171 W.
        v = flat_course_speed_analytic(
            _FLAT_POWER,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            air_density=_FLAT_RHO,
        )
        assert v == pytest.approx(
            _cardano_flat_speed(_FLAT_RHO, _FLAT_CDA, _FLAT_CRR, _FLAT_MASS, _FLAT_POWER),
            rel=1e-12,
        )
        assert v == pytest.approx(9.0973, abs=0.01)

    def test_substitution_recovers_the_power(self) -> None:
        # Substituting v back: 0.5*rho*CdA*v^3 + Crr*m*g*v == eta*P exactly.
        v = flat_course_speed_analytic(
            _FLAT_POWER,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            air_density=_FLAT_RHO,
        )
        left = 0.5 * _FLAT_RHO * _FLAT_CDA * v**3 + _FLAT_CRR * _FLAT_MASS * GRAVITY_M_S2 * v
        assert left == pytest.approx(_FLAT_POWER * DEFAULT_DRIVETRAIN_EFFICIENCY, rel=1e-12)

    def test_limiting_cases_are_exact(self) -> None:
        # No rolling resistance: A v^3 = C -> v = cbrt(2*eta*P/(rho*CdA))
        # (the "cbrt" limit of the quadratic in v^2). No aero (CdA = 0):
        # B v = C -> v = eta*P/(Crr*m*g), linear.
        v_no_crr = flat_course_speed_analytic(
            _FLAT_POWER, cda=_FLAT_CDA, crr=0.0, mass_kg=_FLAT_MASS, air_density=_FLAT_RHO
        )
        assert v_no_crr == pytest.approx(
            (
                2.0 * _FLAT_POWER * DEFAULT_DRIVETRAIN_EFFICIENCY
                / (_FLAT_RHO * _FLAT_CDA)
            ) ** (1.0 / 3.0),
            rel=1e-12,
        )
        v_no_cda = flat_course_speed_analytic(
            _FLAT_POWER, cda=0.0, crr=_FLAT_CRR, mass_kg=_FLAT_MASS, air_density=_FLAT_RHO
        )
        assert v_no_cda == pytest.approx(
            _FLAT_POWER * DEFAULT_DRIVETRAIN_EFFICIENCY / (_FLAT_CRR * _FLAT_MASS * GRAVITY_M_S2),
            rel=1e-12,
        )

    def test_no_resistance_at_all_is_unphysical(self) -> None:
        with pytest.raises(ValueError, match="resist"):
            flat_course_speed_analytic(
                _FLAT_POWER, cda=0.0, crr=0.0, mass_kg=_FLAT_MASS, air_density=_FLAT_RHO
            )


class TestBikeSpeedForPower:
    def test_numeric_solver_matches_hand_solved_flat_example(self) -> None:
        # RP-3 acceptance (§12.8): the numeric solver must match the
        # hand-solved flat-course analytic example.
        analytic = flat_course_speed_analytic(
            _FLAT_POWER,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            air_density=_FLAT_RHO,
        )
        numeric = bike_speed_for_power(
            _FLAT_POWER,
            gradient=0.0,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            air_density=_FLAT_RHO,
        )
        assert numeric == pytest.approx(analytic, rel=1e-9)

    def test_gradient_monotonicity(self) -> None:
        kw = dict(cda=_FLAT_CDA, crr=_FLAT_CRR, mass_kg=_FLAT_MASS, air_density=_FLAT_RHO)
        flat = bike_speed_for_power(_FLAT_POWER, gradient=0.0, **kw)
        up = bike_speed_for_power(_FLAT_POWER, gradient=0.05, **kw)
        down = bike_speed_for_power(_FLAT_POWER, gradient=-0.05, **kw)
        assert down > flat > up
        # Substituting each solved speed back reproduces the target power.
        for gradient, speed in ((0.05, up), (-0.05, down)):
            assert required_bike_power(speed, gradient=gradient, **kw) == pytest.approx(
                _FLAT_POWER, rel=1e-6
            )

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="power"):
            bike_speed_for_power(
                0.0, gradient=0.0, cda=0.3, crr=0.004, mass_kg=80.0, air_density=1.225
            )
        with pytest.raises(ValueError, match="mass"):
            bike_speed_for_power(
                _FLAT_POWER, gradient=0.0, cda=0.3, crr=0.004, mass_kg=0.0, air_density=1.225
            )
        with pytest.raises(ValueError, match="efficiency"):
            bike_speed_for_power(
                _FLAT_POWER,
                gradient=0.0,
                cda=0.3,
                crr=0.004,
                mass_kg=80.0,
                air_density=1.225,
                drivetrain_efficiency=0.0,
            )


class TestDefaultBikePowerFraction:
    def test_distance_dependent_owner_choice_ladder(self) -> None:
        # Sprint 0.95, olympic 0.90, 70.3 0.85, full 0.80 of FTP (documented
        # OWNER CHOICE; the boundaries are the sprint/olympic/half/full
        # bike distances).
        assert default_bike_power_fraction(20000.0) == pytest.approx(0.95)
        assert default_bike_power_fraction(40000.0) == pytest.approx(0.90)
        assert default_bike_power_fraction(90000.0) == pytest.approx(0.85)
        assert default_bike_power_fraction(180000.0) == pytest.approx(0.80)

    def test_invalid_distance_raises(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            default_bike_power_fraction(0.0)


class TestBikeCoursePrediction:
    def test_per_segment_times_and_total(self) -> None:
        result = bike_course_prediction(
            [(40000.0, 0.0), (20000.0, 0.03)],
            ftp_watts=180.0,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            temperature_c=15.0,
            altitude_m=0.0,
        )
        assert isinstance(result, BikeCoursePrediction)
        # 60 km total -> documented olympic fraction 0.90 -> P = 162 W.
        rho = air_density(15.0, 0.0)
        v_flat = bike_speed_for_power(
            162.0, gradient=0.0, cda=_FLAT_CDA, crr=_FLAT_CRR, mass_kg=_FLAT_MASS, air_density=rho
        )
        v_climb = bike_speed_for_power(
            162.0, gradient=0.03, cda=_FLAT_CDA, crr=_FLAT_CRR, mass_kg=_FLAT_MASS, air_density=rho
        )
        assert result.segments[0].point == pytest.approx(40000.0 / v_flat, rel=1e-9)
        assert result.segments[1].point == pytest.approx(20000.0 / v_climb, rel=1e-9)
        assert result.total.point == pytest.approx(
            result.segments[0].point + result.segments[1].point, rel=1e-12
        )

    def test_envelopes_are_ordered_and_wide(self) -> None:
        result = bike_course_prediction(
            [(40000.0, 0.0), (20000.0, 0.03)],
            ftp_watts=180.0,
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            temperature_c=15.0,
            altitude_m=0.0,
        )
        for envelope in (*result.segments, result.total):
            assert envelope.lower < envelope.point < envelope.upper

    def test_explicit_power_and_fraction_are_mutually_exclusive(self) -> None:
        kw = dict(
            cda=_FLAT_CDA,
            crr=_FLAT_CRR,
            mass_kg=_FLAT_MASS,
            temperature_c=15.0,
            altitude_m=0.0,
        )
        with pytest.raises(ValueError, match="power"):
            bike_course_prediction([(1000.0, 0.0)], **kw)  # neither source
        with pytest.raises(ValueError, match="power"):
            bike_course_prediction([(1000.0, 0.0)], ftp_watts=180.0, target_power_w=150.0, **kw)
        with pytest.raises(ValueError, match="segment"):
            bike_course_prediction([], ftp_watts=180.0, **kw)


# ---------------------------------------------------------------------------
# Swim (RP-5): CSS pace with distance fraction and open-water assumption
#
# Hand-computed anchors (CSS = 0.833333 m/s, the owner's real 2:00 /100 m
# from Feature 4's ZON-7 anchor):
#   olympic 1500 m, fraction 1.00, no open-water penalty:
#       T = 1500 / 0.833333 = 1800.0 s exactly
#   with the documented open-water factor 1.03:
#       T = 1800 * 1.03 = 1854.0 s
#   full 3800 m, fraction 0.97, factor 1.03:
#       v = 0.833333 * 0.97 / 1.03 = 0.784790 m/s
#       T = 3800 / 0.784790 = 4842.4 s
# ---------------------------------------------------------------------------


class TestDefaultSwimCssSpeedFraction:
    def test_distance_dependent_owner_choice_ladder(self) -> None:
        assert predict.default_swim_css_speed_fraction(750.0) == pytest.approx(1.05)
        assert predict.default_swim_css_speed_fraction(1500.0) == pytest.approx(1.00)
        assert predict.default_swim_css_speed_fraction(3800.0) == pytest.approx(0.97)

    def test_invalid_distance_raises(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            predict.default_swim_css_speed_fraction(0.0)


class TestSwimPrediction:
    CSS = 200.0 / 240.0  # 0.833333 m/s = 2:00 /100 m (ZON-7 real-world anchor)

    def test_olympic_pool_equivalent_is_exact(self) -> None:
        # Fraction 1.00 (CSS itself) and no open-water penalty: exactly the
        # CSS pace, T = 1500 / 0.833333 = 1800.0 s.
        envelope = predict.swim_prediction(
            1500.0, self.CSS, open_water_factor=1.0
        )
        assert envelope.point == pytest.approx(1800.0, abs=1e-9)

    def test_open_water_factor_is_a_documented_assumption(self) -> None:
        # Default factor 1.03 slows the CSS pace by exactly that factor:
        # T = 1800 * 1.03 = 1854.0 s. The factor is configurable (the
        # assumption is explicit, never buried).
        envelope = predict.swim_prediction(1500.0, self.CSS)
        assert envelope.point == pytest.approx(1800.0 * 1.03, abs=1e-9)
        assert any("ASSUMPTION" in a for a in envelope.assumptions)
        faster = predict.swim_prediction(1500.0, self.CSS, open_water_factor=1.0)
        assert faster.point < envelope.point

    def test_long_course_hand_computed(self) -> None:
        # 3800 m at fraction 0.97 with the 1.03 open-water factor:
        # T = 3800 / (0.833333*0.97/1.03) = 4842.4 s.
        envelope = predict.swim_prediction(3800.0, self.CSS)
        expected = 3800.0 / (self.CSS * 0.97 / 1.03)
        assert envelope.point == pytest.approx(expected, rel=1e-9)
        assert envelope.point == pytest.approx(4842.4, abs=0.5)

    def test_band_from_documented_css_sigma(self) -> None:
        # Band: CSS +/- 2*0.02 relative -> time scales inversely:
        # lower = point / 1.04, upper = point / 0.96.
        envelope = predict.swim_prediction(1500.0, self.CSS)
        assert envelope.lower == pytest.approx(envelope.point / 1.04, rel=1e-9)
        assert envelope.upper == pytest.approx(envelope.point / 0.96, rel=1e-9)
        assert envelope.lower < envelope.point < envelope.upper

    def test_explicit_fraction_overrides_the_ladder(self) -> None:
        envelope = predict.swim_prediction(
            1500.0, self.CSS, css_speed_fraction=1.05, open_water_factor=1.0
        )
        assert envelope.point == pytest.approx(1500.0 / (self.CSS * 1.05), rel=1e-9)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            predict.swim_prediction(0.0, self.CSS)
        with pytest.raises(ValueError, match="CSS"):
            predict.swim_prediction(1500.0, 0.0)
        with pytest.raises(ValueError, match="open_water"):
            predict.swim_prediction(1500.0, self.CSS, open_water_factor=0.99)
        with pytest.raises(ValueError, match="fraction"):
            predict.swim_prediction(1500.0, self.CSS, css_speed_fraction=0.0)


# ---------------------------------------------------------------------------
# Run off the bike (RP-6): personal factor or widened configurable prior
# ---------------------------------------------------------------------------


class TestRunOffBikeFactor:
    def test_insufficient_data_uses_widened_prior(self) -> None:
        # No observations: the documented configurable prior (OWNER CHOICE,
        # 1.06) with the deliberately WIDENED band (+/- 2*0.05 relative).
        envelope = predict.run_off_bike_factor()
        assert envelope.point == pytest.approx(1.06)
        assert envelope.lower == pytest.approx(1.06 * 0.90, rel=1e-9)
        assert envelope.upper == pytest.approx(1.06 * 1.10, rel=1e-9)
        assert envelope.method == "prior_widened"
        assert any("OWNER CHOICE" in a for a in envelope.assumptions)

    def test_one_observation_is_still_insufficient(self) -> None:
        # Below MIN_RUN_OFF_BIKE_OBSERVATIONS (= 2, OWNER CHOICE) a single
        # observation cannot replace the prior.
        assert predict.MIN_RUN_OFF_BIKE_OBSERVATIONS == 2
        envelope = predict.run_off_bike_factor([1.10])
        assert envelope.method == "prior_widened"
        assert envelope.point == pytest.approx(1.06)

    def test_personal_data_gives_mean_with_narrower_band(self) -> None:
        # Three tight observations (1.05, 1.06, 1.07): point = mean = 1.06,
        # band from the standard error of the mean, floored at 0.01 relative
        # -> width 2*2*0.01*1.06 = 0.0424, strictly narrower than the widened
        # prior band (0.212).
        envelope = predict.run_off_bike_factor([1.05, 1.06, 1.07])
        assert envelope.method == "personal_brick_data"
        assert envelope.point == pytest.approx(1.06, abs=1e-12)
        assert envelope.lower == pytest.approx(1.06 * 0.98, rel=1e-9)
        assert envelope.upper == pytest.approx(1.06 * 1.02, rel=1e-9)
        prior = predict.run_off_bike_factor()
        assert (envelope.upper - envelope.lower) < (prior.upper - prior.lower)

    def test_min_observations_is_configurable(self) -> None:
        envelope = predict.run_off_bike_factor([1.10], min_observations=1)
        assert envelope.method == "personal_brick_data"
        assert envelope.point == pytest.approx(1.10)

    def test_no_unsourced_constant_is_hardcoded(self) -> None:
        # RP-6 acceptance: every constant is module-level, named and
        # documented (source citation or OWNER CHOICE marker). The docstring
        # lives on the module variable, so inspect the module source.
        assert pytest.approx(1.06) == predict.RUN_OFF_BIKE_PRIOR_MEAN
        assert pytest.approx(0.05) == predict.RUN_OFF_BIKE_PRIOR_REL_SIGMA
        source = inspect.getsource(predict)
        section = source[source.index("RUN_OFF_BIKE_PRIOR_MEAN"): source.index(
            "PERSONAL_FACTOR_MIN_REL_SIGMA"
        )]
        assert "OWNER CHOICE" in section

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="observation"):
            predict.run_off_bike_factor([1.05, 0.0])
        with pytest.raises(ValueError, match="prior mean"):
            predict.run_off_bike_factor(prior_mean=0.0)
        with pytest.raises(ValueError, match="min_observations"):
            predict.run_off_bike_factor(min_observations=0)


# ---------------------------------------------------------------------------
# Transitions (RP-7): T1/T2 from past races or configurable defaults
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_defaults_per_race_key_are_owner_choice(self) -> None:
        assert predict.transition_defaults_for("sprint") == (180.0, 90.0)
        assert predict.transition_defaults_for("olympic") == (240.0, 120.0)
        assert predict.transition_defaults_for("half") == (300.0, 150.0)
        assert predict.transition_defaults_for("full") == (360.0, 180.0)
        with pytest.raises(ValueError, match="race key"):
            predict.transition_defaults_for("ultra")

    def test_insufficient_data_uses_widened_default_band(self) -> None:
        # No past races: the race-key default with the WIDENED band
        # (+/- 2*0.30 relative) and the generic fallback without a key.
        result = predict.transition_prediction(race_key="olympic")
        assert result.t1.point == pytest.approx(240.0)
        assert result.t1.lower == pytest.approx(240.0 * 0.40, rel=1e-9)
        assert result.t1.upper == pytest.approx(240.0 * 1.60, rel=1e-9)
        assert result.t1.method == "default_t1_prior"
        assert result.t2.point == pytest.approx(120.0)
        assert result.t2.method == "default_t2_prior"
        generic = predict.transition_prediction()
        assert generic.t1.point == pytest.approx(300.0)
        assert generic.t2.point == pytest.approx(150.0)

    def test_past_races_give_median_with_narrower_band(self) -> None:
        result = predict.transition_prediction(
            [230.0, 240.0, 250.0, 260.0], race_key="olympic"
        )
        assert result.t1.method == "observed_t1_median"
        assert result.t1.point == pytest.approx(245.0)  # median of four
        prior = predict.transition_prediction(race_key="olympic")
        assert (result.t1.upper - result.t1.lower) < (
            prior.t1.upper - prior.t1.lower
        )
        # T2 still on the prior (no observations passed).
        assert result.t2.method == "default_t2_prior"

    def test_explicit_defaults_override_the_race_key(self) -> None:
        result = predict.transition_prediction(
            race_key="olympic", default_t1_s=300.0, default_t2_s=150.0
        )
        assert result.t1.point == pytest.approx(300.0)
        assert result.t2.point == pytest.approx(150.0)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="past t1"):
            predict.transition_prediction([-5.0])
        with pytest.raises(ValueError, match="default t1"):
            predict.transition_prediction(default_t1_s=0.0)


# ---------------------------------------------------------------------------
# Composite triathlon total (RP-8): Monte Carlo over documented distributions
# ---------------------------------------------------------------------------

_TRIATHLON_KW: dict[str, Any] = dict(
    reference_run_distance_m=5000.0,
    reference_run_time_s=1200.0,
    run_distance_m=10000.0,
    swim_distance_m=1500.0,
    css_mps=200.0 / 240.0,
    bike_segments=[(40000.0, 0.0)],
    ftp_watts=180.0,
    cda=0.30,
    crr=0.004,
    total_mass_kg=80.0,
    temperature_c=15.0,
    altitude_m=0.0,
    race_key="olympic",
)


class TestTriathlonTotalPrediction:
    def test_total_point_is_the_sum_of_the_nominal_segments(self) -> None:
        result = predict.triathlon_total_prediction(
            **_TRIATHLON_KW, n_samples=200, seed=1
        )
        expected = (
            result.swim.point
            + result.bike.total.point
            + result.run.point
            + result.transitions.t1.point
            + result.transitions.t2.point
        )
        assert result.total.point == pytest.approx(expected, rel=1e-9)
        # Olympic-distance sanity (hand-checked magnitudes): swim 1854 s,
        # 40 km flat bike at 0.90*180 W ~ 4487 s, run 2501.9*1.06 ~ 2652 s,
        # transitions 240 + 120 -> total ~ 9353 s = 2:35:53.
        assert result.total.point == pytest.approx(9353.5, abs=2.0)

    def test_bounds_are_stable_under_a_fixed_seed(self) -> None:
        # RP-8 acceptance: same seed -> IDENTICAL bounds (exact equality,
        # not approx: the rng path is fully determined).
        first = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=200, seed=42)
        second = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=200, seed=42)
        assert first.total == second.total
        assert first.swim == second.swim
        assert first.run == second.run
        assert first.bike.total == second.bike.total

    def test_different_seed_gives_different_bounds(self) -> None:
        first = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=200, seed=1)
        second = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=200, seed=2)
        assert first.total.upper != second.total.upper

    def test_segments_and_total_are_ordered(self) -> None:
        result = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=200, seed=7)
        for envelope in (
            result.swim,
            *result.bike.segments,
            result.bike.total,
            result.run,
            result.transitions.t1,
            result.transitions.t2,
            result.total,
        ):
            assert envelope.lower < envelope.point < envelope.upper

    def test_documented_input_distributions_are_stated(self) -> None:
        # Every sampled distribution must be a documented assumption in the
        # envelope it feeds (section 7.7).
        result = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=200, seed=7)
        for envelope in (
            result.swim,
            *result.bike.segments,
            result.run,
            result.transitions.t1,
            result.total,
        ):
            assert any("OWNER CHOICE" in a or "assumption" in a for a in envelope.assumptions)

    def test_personal_brick_data_narrows_the_run_segment(self) -> None:
        # Passing real brick observations replaces the widened prior.
        prior = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=400, seed=7)
        personal = predict.triathlon_total_prediction(
            **_TRIATHLON_KW,
            brick_factor_observations=[1.05, 1.06, 1.07],
            n_samples=400,
            seed=7,
        )
        assert (personal.run.upper - personal.run.lower) < (
            prior.run.upper - prior.run.lower
        )

    def test_vectorized_solver_agrees_with_scalar_solver(self) -> None:
        # The Monte Carlo path uses a vectorized bisection; at nominal inputs
        # it must agree with the public scalar solver (guards divergence).
        rho = predict.air_density(15.0, 0.0)
        speeds = predict._solve_speeds_vectorized(
            np.asarray([162.0]),
            gradient=0.0,
            cda=np.asarray([0.30]),
            crr=np.asarray([0.004]),
            mass_kg=80.0,
            air_density=rho,
        )
        scalar = predict.bike_speed_for_power(
            162.0, gradient=0.0, cda=0.30, crr=0.004, mass_kg=80.0, air_density=rho
        )
        assert float(speeds[0]) == pytest.approx(scalar, rel=1e-9)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="n_samples"):
            predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=0)
        with pytest.raises(ValueError, match="gradient"):
            predict.triathlon_total_prediction(
                **{**_TRIATHLON_KW, "bike_segments": [(1000.0, -1.5)]}
            )


# ---------------------------------------------------------------------------
# Envelope contract (RP-9, sections 7.7 / 12.8): every prediction — segment
# or total — returns exactly {point, lower, upper, method, assumptions,
# model_version} with lower <= point <= upper.
# ---------------------------------------------------------------------------


def _all_produced_envelopes() -> list[predict.PredictionEnvelope]:
    """Walk EVERY prediction function's output (segment and total)."""
    run = predict.predict_run(5000.0, 1200.0, 10000.0)
    bike = predict.bike_course_prediction(
        [(40000.0, 0.0), (10000.0, 0.02)],
        ftp_watts=180.0,
        cda=0.30,
        crr=0.004,
        mass_kg=80.0,
        temperature_c=15.0,
        altitude_m=0.0,
    )
    swim = predict.swim_prediction(1500.0, 200.0 / 240.0)
    factor = predict.run_off_bike_factor()
    transitions = predict.transition_prediction(race_key="olympic")
    tri = predict.triathlon_total_prediction(**_TRIATHLON_KW, n_samples=100, seed=1)
    return [
        run.riegel,
        run.vdot,
        *bike.segments,
        bike.total,
        swim,
        factor,
        transitions.t1,
        transitions.t2,
        tri.swim,
        *tri.bike.segments,
        tri.bike.total,
        tri.run,
        tri.transitions.t1,
        tri.transitions.t2,
        tri.total,
    ]


class TestEnvelopeContract:
    def test_every_producer_returns_exact_six_field_envelope(self) -> None:
        for envelope in _all_produced_envelopes():
            assert isinstance(envelope, predict.PredictionEnvelope)
            assert tuple(f.name for f in fields(envelope)) == (
                "point",
                "lower",
                "upper",
                "method",
                "assumptions",
                "model_version",
            )
            assert envelope.lower <= envelope.point <= envelope.upper
            assert envelope.assumptions, "a prediction must state assumptions"
            assert envelope.method
            assert envelope.model_version == PREDICTION_MODEL_VERSION

    def test_producer_walk_covers_every_prediction_function(self) -> None:
        # Guard against a new prediction function forgetting the contract:
        # the walk must include every method tag emitted by the module.
        method_tags = {e.method for e in _all_produced_envelopes()}
        assert {
            "riegel_1981",
            "vdot_inversion_daniels_gilbert",
            "martin_1998_numeric_bisection",
            "martin_1998_numeric_bisection_course_sum",
            "css_fraction_open_water",
            "prior_widened",
            "default_t1_prior",
            "default_t2_prior",
            "monte_carlo_composite",
        } <= method_tags

    def test_inverted_band_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="lower <= point <= upper"):
            predict.PredictionEnvelope(
                point=1.0,
                lower=2.0,
                upper=3.0,
                method="x",
                assumptions=("a",),
                model_version="v",
            )

    def test_empty_assumptions_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="assumptions"):
            predict.PredictionEnvelope(
                point=1.0,
                lower=0.5,
                upper=1.5,
                method="x",
                assumptions=(),
                model_version="v",
            )
