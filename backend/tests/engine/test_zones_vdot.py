"""Hand-computed reference tests for VDOT and Daniels training paces.

ZON-6 (RED+GREEN) of ``odd/tasks/engine-zones.md`` (PROJECT_BRIEF section
7.3): the Daniels & Gilbert VDOT formulas implemented verbatim and
verified against hand-computed reference values, plus the training-pace
derivation (easy, marathon, threshold, interval, repetition).

Formulas (Daniels & Gilbert; PROJECT_BRIEF section 7.3; Daniels,
"Daniels' Running Formula"):

    VO2      = -4.60 + 0.182258 * v + 0.000104 * v^2      (v in m/min)
    %VO2max  = 0.8 + 0.1894393 * e^(-0.012778 t)
                  + 0.2989558 * e^(-0.1932605 t)          (t in min)
    VDOT     = VO2 / %VO2max

Reference anchors (hand arithmetic, digits from an exact evaluation of
the formulas above):

1. 5000 m in 20:00 (v = 5000/20 = 250 m/min, t = 20 min):
       VO2     = -4.60 + 0.182258*250 + 0.000104*250^2
               = -4.60 + 45.5645 + 6.5 = 47.4645
       %VO2max = 0.8 + 0.1894393*e^(-0.25556) + 0.2989558*e^(-3.86521)
               = 0.8 + 0.146719 + 0.006264 = 0.952983
       VDOT    = 47.4645 / 0.952983 = 49.806
   Daniels' published VDOT table places 20:00 for 5000 m at VDOT ~= 50,
   so 49.81 is the expected cross-check (delta < 0.2).

2. 10,000 m in 40:00 (same v = 250 m/min, t = 40 min):
       VO2     = 47.4645 (identical velocity)
       %VO2max = 0.8 + 0.1894393*e^(-0.51112) + 0.2989558*e^(-7.73042)
               = 0.8 + 0.113630 + 0.000131 = 0.913761
       VDOT    = 47.4645 / 0.913761 = 51.944
   Daniels' table places 40:00 for 10,000 m at VDOT ~= 51.9-52.0.

3. 1500 m in 4:00 (v = 375 m/min, t = 4 min):
       VO2     = -4.60 + 0.182258*375 + 0.000104*375^2
               = -4.60 + 68.34675 + 14.625 = 78.37175
       %VO2max = 0.8 + 0.1894393*e^(-0.051112) + 0.2989558*e^(-0.773042)
               = 0.8 + 0.180008 + 0.137992 = 1.118000
       VDOT    = 78.37175 / 1.118000 = 70.100
   Daniels' table places 4:00 for 1500 m at VDOT ~= 70.0.

Velocity inversion (for training paces): the VO2 equation is quadratic in
v, so the velocity at a target VO2 is its POSITIVE root:

    v = (-0.182258 + sqrt(0.182258^2 + 4 * 0.000104 * (4.60 + target)))
        / (2 * 0.000104)

Hand check: target = VO2(250) = 47.4645 recovers v = 250 m/min exactly.

Training paces: each pace targets a fixed percentage of VO2max (Daniels'
intensity table); its velocity is the positive root at
``pct * VDOT``. Threshold cross-check at VDOT = 50 with the default
85.5%: target = 42.75 -> v = 229.6917 m/min = 3.8282 m/s
= 261.22 s/km = 4:21.2 /km, matching Daniels' published VDOT-50
threshold pace of ~4:21 /km.

Real-data note: the owner has NO run threshold pace configured in
Intervals.icu, so VDOT is only exercised against hand-computed reference
values here; once the owner logs real efforts these anchors gate every
implementation change.
"""

from dataclasses import FrozenInstanceError

import pytest

from app.engine.zones import (
    TrainingPace,
    VdotPaces,
    percent_vo2max,
    training_paces,
    vdot_from_effort,
    velocity_from_vo2,
    vo2_from_velocity,
)


class TestVo2FromVelocity:
    def test_anchor_250_m_per_min_hand_computed(self) -> None:
        # -4.60 + 0.182258*250 + 0.000104*250^2 = 47.4645 (docstring anchor 1).
        assert vo2_from_velocity(250.0) == pytest.approx(47.4645, abs=1e-9)

    def test_anchor_375_m_per_min_hand_computed(self) -> None:
        # -4.60 + 0.182258*375 + 0.000104*375^2 = 78.37175 (docstring anchor 3).
        assert vo2_from_velocity(375.0) == pytest.approx(78.37175, abs=1e-9)

    def test_non_positive_velocity_raises(self) -> None:
        with pytest.raises(ValueError, match="velocity"):
            vo2_from_velocity(0.0)
        with pytest.raises(ValueError, match="velocity"):
            vo2_from_velocity(-250.0)


class TestPercentVo2max:
    def test_anchor_20_min_hand_computed(self) -> None:
        # 0.8 + 0.1894393*e^(-0.25556) + 0.2989558*e^(-3.86521) = 0.952983...
        assert percent_vo2max(20.0) == pytest.approx(0.952983, abs=1e-5)

    def test_anchor_4_min_hand_computed(self) -> None:
        # 0.8 + 0.1894393*e^(-0.051112) + 0.2989558*e^(-0.773042) = 1.118000...
        assert percent_vo2max(4.0) == pytest.approx(1.118000, abs=1e-5)

    def test_long_effort_declines_toward_asymptote(self) -> None:
        # The exponentials decay: 60 min must be below 20 min and above 0.8.
        assert 0.8 < percent_vo2max(60.0) < percent_vo2max(20.0)

    def test_non_positive_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            percent_vo2max(0.0)
        with pytest.raises(ValueError, match="duration"):
            percent_vo2max(-20.0)


class TestVdotFromEffort:
    def test_anchor_5000m_20min_is_49_8(self) -> None:
        # 5000 m in 20:00 -> v = 250 m/min -> VDOT = 47.4645/0.952983 = 49.806
        # (docstring anchor 1); Daniels' table says ~= 50.
        vdot = vdot_from_effort(5000.0, 1200.0)
        assert vdot == pytest.approx(49.806, abs=5e-3)
        assert vdot == pytest.approx(50.0, abs=0.2)

    def test_anchor_10000m_40min_is_51_9(self) -> None:
        # 10,000 m in 40:00 -> same v = 250 m/min -> VDOT = 51.944 (anchor 2).
        assert vdot_from_effort(10000.0, 2400.0) == pytest.approx(51.944, abs=5e-3)

    def test_anchor_1500m_4min_is_70_1(self) -> None:
        # 1500 m in 4:00 -> v = 375 m/min -> VDOT = 78.37175/1.118000 = 70.100.
        assert vdot_from_effort(1500.0, 240.0) == pytest.approx(70.100, abs=5e-3)

    def test_longer_same_pace_effort_scores_higher(self) -> None:
        # 40 min at the same velocity is a bigger physiological achievement
        # than 20 min (smaller %VO2max), so the VDOT must be higher.
        assert vdot_from_effort(10000.0, 2400.0) > vdot_from_effort(5000.0, 1200.0)

    def test_non_positive_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="distance"):
            vdot_from_effort(0.0, 1200.0)
        with pytest.raises(ValueError, match="distance"):
            vdot_from_effort(-5000.0, 1200.0)
        with pytest.raises(ValueError, match="duration"):
            vdot_from_effort(5000.0, 0.0)
        with pytest.raises(ValueError, match="duration"):
            vdot_from_effort(5000.0, -1200.0)


class TestVelocityFromVo2:
    def test_round_trip_recovers_250_m_per_min(self) -> None:
        # Positive root at target = VO2(250) = 47.4645 must be 250 m/min
        # (docstring hand check) — and not the negative root (-1761.35...).
        assert velocity_from_vo2(vo2_from_velocity(250.0)) == pytest.approx(250.0, rel=1e-9)

    def test_result_is_the_positive_root(self) -> None:
        # Direct quadratic check: substituting v back reproduces the target.
        target = 42.75
        v = velocity_from_vo2(target)
        assert v > 0
        assert vo2_from_velocity(v) == pytest.approx(target, rel=1e-9)

    def test_non_positive_target_raises(self) -> None:
        # A target at or below zero has no physically meaningful velocity.
        with pytest.raises(ValueError, match="target VO2"):
            velocity_from_vo2(0.0)
        with pytest.raises(ValueError, match="target VO2"):
            velocity_from_vo2(-10.0)


class TestTrainingPaces:
    def test_five_paces_present_and_ordered(self) -> None:
        paces = training_paces(50.0)
        assert paces.vdot == pytest.approx(50.0)
        for pace in (paces.easy, paces.marathon, paces.threshold, paces.interval, paces.repetition):
            assert isinstance(pace, TrainingPace)
        speeds = [
            paces.easy.speed_mps,
            paces.marathon.speed_mps,
            paces.threshold.speed_mps,
            paces.interval.speed_mps,
            paces.repetition.speed_mps,
        ]
        assert speeds == sorted(speeds)
        assert all(s > 0 for s in speeds)

    def test_threshold_pace_hand_computed_at_vdot_50(self) -> None:
        # Default 85.5% of VDOT 50 -> target 42.75 -> 229.6917 m/min
        # = 3.8282 m/s = 261.22 s/km = 4:21.2 /km (docstring cross-check
        # against Daniels' published VDOT-50 threshold pace ~4:21 /km).
        pace = training_paces(50.0).threshold
        assert pace.speed_mps == pytest.approx(3.8282, abs=5e-4)
        assert pace.pace_sec_per_km == pytest.approx(261.22, abs=0.05)
        assert pace.pace_per_km == "4:21 /km"

    def test_easy_pace_hand_computed_at_vdot_50(self) -> None:
        # Default 66.5% of VDOT 50 -> target 33.25 -> 187.5921 m/min
        # = 3.126535 m/s = 319.84 s/km = 5:19.8 /km.
        pace = training_paces(50.0).easy
        assert pace.speed_mps == pytest.approx(3.126535, abs=5e-4)
        assert pace.pace_sec_per_km == pytest.approx(319.84, abs=0.05)

    def test_pct_vo2max_recorded_on_each_pace(self) -> None:
        paces = training_paces(50.0)
        assert paces.easy.pct_vo2max == pytest.approx(66.5)
        assert paces.repetition.pct_vo2max == pytest.approx(112.5)

    def test_band_choices_are_configurable(self) -> None:
        # Every band percentage is an explicit, owner-reviewable parameter.
        paces = training_paces(50.0, threshold_pct=88.0)
        assert paces.threshold.pct_vo2max == pytest.approx(88.0)
        assert paces.threshold.speed_mps > training_paces(50.0).threshold.speed_mps

    def test_non_positive_vdot_raises(self) -> None:
        with pytest.raises(ValueError, match="vdot"):
            training_paces(0.0)
        with pytest.raises(ValueError, match="vdot"):
            training_paces(-50.0)

    def test_target_out_of_physical_range_raises(self) -> None:
        # A band choice that drives the target VO2 to zero or below has no
        # physically meaningful velocity: raise, never return a negative or
        # imaginary velocity.
        with pytest.raises(ValueError, match="target VO2"):
            training_paces(50.0, easy_pct=0.0)
        with pytest.raises(ValueError, match="target VO2"):
            training_paces(50.0, easy_pct=-10.0)
        with pytest.raises(ValueError, match="target VO2"):
            training_paces(1.0, easy_pct=-5.0)

    def test_result_is_frozen(self) -> None:
        paces = training_paces(50.0)
        with pytest.raises(FrozenInstanceError):
            paces.vdot = 99.0  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            paces.threshold.speed_mps = 99.0  # type: ignore[misc]

    def test_is_vdot_paces_instance(self) -> None:
        assert isinstance(training_paces(50.0), VdotPaces)
