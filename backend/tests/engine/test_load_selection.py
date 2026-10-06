"""Method-selection rule tests (ODD LOAD-6, PROJECT_BRIEF section 7.1).

The selection order is fixed: power -> pace/speed -> HR -> sRPE. Every
expected load value below reuses the reference-value arithmetic already
unit-tested in ``test_load_power.py`` / ``test_load_pace_speed.py`` /
``test_load_hr.py``; these tests assert selection and traceability, not new
math. Owner thresholds used throughout (ODD engine-load Progress notes):

    FTP 180 W, LTHR 169 bpm, HRmax 186 bpm, HRrest 65 bpm,
    swim CSS 0.8333 m/s, (no run threshold pace configured - owner gap;
    tests use an explicit hypothetical 3.0 m/s for pace-selection cases).

Hand arithmetic reused:

    hrTSS 60min@150 (male) = 60.4576391242
    sRPE 7 x 60 min x 0.5  = 210.0 TSS-equivalent
    1 h at FTP (bike)      = 100 TSS
    1 h at threshold (run/swim pace) = 100 rTSS / sTSS
"""

from typing import Final

import pytest

from app.engine.load import (
    STRENGTH_SPORTS,
    ActivityLoadInput,
    BikePowerLoad,
    RunPaceLoad,
    SwimPaceLoad,
    ThresholdBundle,
    TrimpCoefficients,
    is_strength_sport,
    run_pace_load,
    select_load_method,
)

COEFF: Final = TrimpCoefficients.from_sex("male")

FULL_THRESHOLDS: Final = ThresholdBundle(
    ftp_watts=180.0,
    threshold_run_speed_mps=3.0,
    css_mps=0.8333,
    lthr_bpm=169.0,
    hr_max_bpm=186.0,
    hr_rest_bpm=65.0,
    srpe_tss_equivalent_factor=0.5,
)


def constant_stream(value: float, length: int) -> list[float]:
    return [value] * length


class TestFixedOrderPrecedence:
    """Several applicable methods: the earliest in the fixed order wins."""

    def test_ride_with_power_hr_and_rpe_selects_power(self) -> None:
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            power_samples=constant_stream(180.0, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "power"
        # 1 h at NP = FTP (180 W) with FTP 180 W scores exactly 100 TSS.
        assert selection.tss == pytest.approx(100.0)
        # Nothing was skipped: power is the first method in the fixed order.
        assert selection.skipped == {}

    def test_run_with_pace_hr_and_rpe_selects_pace_speed(self) -> None:
        activity = ActivityLoadInput(
            sport="Run",
            duration_s=3600.0,
            speed_samples=constant_stream(3.0, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "pace_speed"
        # 1 h at threshold run speed scores exactly 100 rTSS.
        assert selection.tss == pytest.approx(100.0)
        # Power was evaluated and skipped: it only applies to cycling sports.
        assert set(selection.skipped) == {"power"}

    def test_swim_with_css_hr_and_rpe_selects_pace_speed(self) -> None:
        activity = ActivityLoadInput(
            sport="Swim",
            duration_s=3600.0,
            speed_samples=constant_stream(0.8333, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "pace_speed"
        # 1 h at CSS scores exactly 100 sTSS (IF = 1, cubic exponent).
        assert selection.tss == pytest.approx(100.0)
        assert set(selection.skipped) == {"power"}


class TestPerSportApplicability:
    def test_powerless_ride_selects_hr_not_speed(self) -> None:
        """Owner reality: no power meter; speed is NOT a bike load method."""
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            speed_samples=constant_stream(8.0, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert selection.tss == pytest.approx(60.4576391242)
        assert "power" in selection.skipped
        assert "pace_speed" in selection.skipped
        # The pace/speed skip reason must name why cycling cannot use speed.
        assert "cycling" in selection.skipped["pace_speed"]
        assert "power" in selection.skipped["power"]

    def test_power_requires_positive_ftp(self) -> None:
        thresholds = ThresholdBundle(
            ftp_watts=None,
            threshold_run_speed_mps=3.0,
            css_mps=0.8333,
            lthr_bpm=169.0,
            hr_max_bpm=186.0,
            hr_rest_bpm=65.0,
            srpe_tss_equivalent_factor=0.5,
        )
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            power_samples=constant_stream(180.0, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, thresholds, coefficients=COEFF)
        assert selection.method == "hr"
        assert "FTP" in selection.skipped["power"]

    def test_power_rejects_non_positive_ftp(self) -> None:
        thresholds = ThresholdBundle(
            ftp_watts=0.0,
            threshold_run_speed_mps=3.0,
            css_mps=0.8333,
            lthr_bpm=169.0,
            hr_max_bpm=186.0,
            hr_rest_bpm=65.0,
            srpe_tss_equivalent_factor=0.5,
        )
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            power_samples=constant_stream(180.0, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, thresholds, coefficients=COEFF)
        assert selection.method == "hr"
        assert "FTP" in selection.skipped["power"]

    def test_run_without_threshold_pace_selects_hr_and_reports_gap(self) -> None:
        """Owner gap: no run threshold pace configured in Intervals.icu."""
        thresholds = ThresholdBundle(
            ftp_watts=180.0,
            threshold_run_speed_mps=None,
            css_mps=0.8333,
            lthr_bpm=169.0,
            hr_max_bpm=186.0,
            hr_rest_bpm=65.0,
            srpe_tss_equivalent_factor=0.5,
        )
        activity = ActivityLoadInput(
            sport="Run",
            duration_s=3600.0,
            speed_samples=constant_stream(3.0, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, thresholds, coefficients=COEFF)
        assert selection.method == "hr"
        assert "threshold" in selection.skipped["pace_speed"].lower()

    def test_swim_without_css_selects_hr(self) -> None:
        thresholds = ThresholdBundle(
            ftp_watts=180.0,
            threshold_run_speed_mps=3.0,
            css_mps=None,
            lthr_bpm=169.0,
            hr_max_bpm=186.0,
            hr_rest_bpm=65.0,
            srpe_tss_equivalent_factor=0.5,
        )
        activity = ActivityLoadInput(
            sport="Swim",
            duration_s=3600.0,
            speed_samples=constant_stream(0.8333, 3600),
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, thresholds, coefficients=COEFF)
        assert selection.method == "hr"
        assert "CSS" in selection.skipped["pace_speed"]

    def test_hr_requires_lthr_max_and_rest(self) -> None:
        thresholds = ThresholdBundle(
            ftp_watts=180.0,
            threshold_run_speed_mps=3.0,
            css_mps=0.8333,
            lthr_bpm=None,
            hr_max_bpm=None,
            hr_rest_bpm=None,
            srpe_tss_equivalent_factor=0.5,
        )
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, thresholds, coefficients=COEFF)
        assert selection.method == "srpe"
        assert selection.tss == pytest.approx(210.0)
        assert "threshold" in selection.skipped["hr"].lower()

    def test_hr_requires_average_hr(self) -> None:
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "srpe"
        assert "HR" in selection.skipped["hr"]

    def test_degenerate_hr_range_falls_through_to_srpe(self) -> None:
        thresholds = ThresholdBundle(
            ftp_watts=180.0,
            threshold_run_speed_mps=3.0,
            css_mps=0.8333,
            lthr_bpm=169.0,
            hr_max_bpm=65.0,
            hr_rest_bpm=65.0,
            srpe_tss_equivalent_factor=0.5,
        )
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, thresholds, coefficients=COEFF)
        assert selection.method == "srpe"
        assert "HRmax" in selection.skipped["hr"]

    def test_strength_session_uses_srpe(self) -> None:
        activity = ActivityLoadInput(
            sport="WeightTraining",
            duration_s=3600.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "srpe"
        assert selection.tss == pytest.approx(210.0)

    @pytest.mark.parametrize("sport", ["Walk", "Hike", "Snowshoe"])
    def test_walking_sports_are_known_and_use_the_hr_path(self, sport: str) -> None:
        """Real-data gap found on the owner's account (LOAD-10): 4 activities
        are typed ``Walk`` and were rejected as unknown sports, losing their
        load. Walking/hiking are legitimate aerobic load for a triathlete, so
        the Strava-style walking types are known and reach the HR-based TRIMP
        path (no walk-specific pace threshold exists; sRPE stays the last
        resort). 60 min @ 150 bpm reuses the hand-calculated hrTSS value."""
        activity = ActivityLoadInput(
            sport=sport,
            duration_s=3600.0,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert selection.tss == pytest.approx(60.4576391242)
        # Power and pace/speed were evaluated and skipped before HR won;
        # the walk reason must not be a silent fall-through.
        assert "power" in selection.skipped
        assert "pace_speed" in selection.skipped

    def test_walk_with_hr_and_no_rpe_still_needs_a_method(self) -> None:
        """A known walking sport without HR and without RPE still raises the
        explicit no-applicable-method error (known sport != decidable)."""
        activity = ActivityLoadInput(sport="Walk", duration_s=3600.0)
        with pytest.raises(ValueError, match="no applicable load method"):
            select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)


class TestNothingApplicable:
    def test_empty_ride_raises_value_error(self) -> None:
        activity = ActivityLoadInput(sport="Ride", duration_s=3600.0)
        with pytest.raises(ValueError, match="no applicable load method"):
            select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)

    def test_error_message_lists_every_skip_reason(self) -> None:
        activity = ActivityLoadInput(sport="Ride", duration_s=3600.0)
        with pytest.raises(ValueError) as excinfo:
            select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        message = str(excinfo.value)
        for key in ("power", "pace_speed", "hr", "srpe"):
            assert key in message

    def test_non_positive_duration_raises(self) -> None:
        activity = ActivityLoadInput(sport="WeightTraining", duration_s=0.0, rpe=7.0)
        with pytest.raises(ValueError, match="duration"):
            select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)

    def test_unknown_sport_raises(self) -> None:
        activity = ActivityLoadInput(sport="BogusSport", duration_s=3600.0, rpe=7.0)
        with pytest.raises(ValueError, match="BogusSport"):
            select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)


class TestMissingSamplesConsistency:
    """Missing/None samples behave consistently with the existing methods."""

    def test_all_none_power_samples_skip_to_hr(self) -> None:
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            power_samples=[None] * 600,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert "no usable power" in selection.skipped["power"].lower()

    def test_all_none_run_speed_samples_skip_to_hr(self) -> None:
        activity = ActivityLoadInput(
            sport="Run",
            duration_s=3600.0,
            speed_samples=[None] * 600,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert "no usable speed" in selection.skipped["pace_speed"].lower()

    def test_swim_without_positive_speed_sample_skips_to_hr(self) -> None:
        # Rest intervals (zero speed) are not swimming: no usable swim data.
        activity = ActivityLoadInput(
            sport="Swim",
            duration_s=3600.0,
            speed_samples=[0.0] * 600,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert "no usable swim" in selection.skipped["pace_speed"].lower()

    def test_none_speed_samples_field_is_same_as_absent(self) -> None:
        absent = ActivityLoadInput(sport="Run", duration_s=3600.0, hr_avg_bpm=150.0, rpe=7.0)
        explicit_none = ActivityLoadInput(
            sport="Run", duration_s=3600.0, speed_samples=None, hr_avg_bpm=150.0, rpe=7.0
        )
        a = select_load_method(absent, FULL_THRESHOLDS, coefficients=COEFF)
        b = select_load_method(explicit_none, FULL_THRESHOLDS, coefficients=COEFF)
        assert a.method == b.method == "hr"
        assert a.tss == pytest.approx(b.tss)


class TestResultShape:
    """The result reports method key, load value, method detail and skips."""

    def test_method_keys_are_stable_machine_readable(self) -> None:
        ride = ActivityLoadInput(
            sport="Ride", duration_s=3600.0, power_samples=constant_stream(180.0, 3600)
        )
        assert select_load_method(ride, FULL_THRESHOLDS, coefficients=COEFF).method == "power"
        run = ActivityLoadInput(
            sport="Run", duration_s=3600.0, speed_samples=constant_stream(3.0, 3600)
        )
        assert select_load_method(run, FULL_THRESHOLDS, coefficients=COEFF).method == "pace_speed"
        hr_ride = ActivityLoadInput(sport="Ride", duration_s=3600.0, hr_avg_bpm=150.0)
        assert select_load_method(hr_ride, FULL_THRESHOLDS, coefficients=COEFF).method == "hr"
        strength = ActivityLoadInput(sport="WeightTraining", duration_s=3600.0, rpe=7.0)
        assert select_load_method(strength, FULL_THRESHOLDS, coefficients=COEFF).method == "srpe"

    def test_power_detail_is_bike_power_load(self) -> None:
        activity = ActivityLoadInput(
            sport="Ride", duration_s=3600.0, power_samples=constant_stream(180.0, 3600)
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert isinstance(selection.detail, BikePowerLoad)
        expected = 100.0  # 1 h at FTP
        assert selection.detail.tss == pytest.approx(expected)
        assert selection.tss == pytest.approx(selection.detail.tss)

    def test_run_detail_is_run_pace_load_with_stream_passthrough(self) -> None:
        # Uphill: 1 m/s over +10% grade for 1 h; grade streams passed through
        # unchanged to run_pace_load (math owned by LOAD-3).
        n = 3600
        speed = constant_stream(1.0, n)
        distance = [float(i) for i in range(n)]  # 1 m per sample
        altitude = [0.0] * 1800 + [float(i - 1799) * 0.1 for i in range(1800, n)]
        activity = ActivityLoadInput(
            sport="Run",
            duration_s=3600.0,
            speed_samples=speed,
            distance_samples=distance,
            altitude_samples=altitude,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert isinstance(selection.detail, RunPaceLoad)
        expected = run_pace_load(
            speed,
            duration_s=3600.0,
            threshold_run_speed=3.0,
            distance_samples=distance,
            altitude_samples=altitude,
        )
        assert selection.detail == expected
        assert selection.tss == pytest.approx(expected.tss)

    def test_swim_detail_is_swim_pace_load(self) -> None:
        activity = ActivityLoadInput(
            sport="Swim", duration_s=3600.0, speed_samples=constant_stream(0.8333, 3600)
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert isinstance(selection.detail, SwimPaceLoad)
        assert selection.detail.tss == pytest.approx(100.0)

    def test_hr_detail_is_dhr_ratio(self) -> None:
        activity = ActivityLoadInput(sport="Ride", duration_s=3600.0, hr_avg_bpm=150.0)
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert isinstance(selection.detail, float)
        # dHRr@150 = (150 - 65) / (186 - 65) = 0.7024793388
        assert selection.detail == pytest.approx(0.7024793388)

    def test_srpe_detail_is_raw_foster_load(self) -> None:
        activity = ActivityLoadInput(sport="WeightTraining", duration_s=3600.0, rpe=7.0)
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert isinstance(selection.detail, float)
        assert selection.detail == pytest.approx(420.0)  # 7 x 60 min

    def test_skip_reasons_cover_every_earlier_method(self) -> None:
        activity = ActivityLoadInput(sport="Ride", duration_s=3600.0, rpe=7.0)
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "srpe"
        assert set(selection.skipped) == {"power", "pace_speed", "hr"}
        for reason in selection.skipped.values():
            assert isinstance(reason, str) and reason


class TestStrengthSportClassification:
    """Public engine helper: which sports are strength sessions (LOAD-10).

    The Intervals.icu PMC cross-check must exclude exactly the sports the
    engine treats as strength when building its aerobic-only load view;
    the classification lives here so the tool imports it instead of
    duplicating the literals.
    """

    def test_known_strength_types_case_insensitive(self) -> None:
        for sport in ("weighttraining", "strengthworkout", "workout"):
            assert is_strength_sport(sport)
        assert is_strength_sport("WeightTraining")
        assert is_strength_sport("STRENGTHWORKOUT")
        assert is_strength_sport("Workout")

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert is_strength_sport("  Workout ")

    def test_aerobic_sports_are_not_strength(self) -> None:
        for sport in ("Ride", "Run", "Swim", "Walk", "Hike", "VirtualRide"):
            assert not is_strength_sport(sport)

    def test_unknown_sport_is_not_strength(self) -> None:
        # is_strength_sport classifies; it does not validate vocabulary.
        # Strict validation stays in select_load_method (ValueError there).
        assert not is_strength_sport("Windsurf")

    def test_strength_sports_are_part_of_the_known_vocabulary(self) -> None:
        # The strength types must stay recognised sports (a typo here would
        # silently make select_load_method reject real strength sessions).
        assert {"weighttraining", "strengthworkout", "workout"} == STRENGTH_SPORTS


class TestStrengthSrpePreference:
    """LOAD-12: for strength sports an owner-entered RPE chooses sRPE
    BEFORE the generic HR step.

    Explicit decision (brief §7.1 assigns sRPE to strength; HR is not a
    valid strength-load proxy — the owner's real gym sessions average
    81-103 bpm, so TRIMP/hrTSS would rate a hard lift as near-rest):
    the generic power -> pace/speed -> HR -> sRPE chain is INVERTED for
    strength sports only: sRPE wins whenever an RPE is recorded, and HR
    is only the fallback when no RPE exists. Every other sport's order is
    untouched, and the bypass stays traceable in ``skipped``.
    """

    def test_strength_with_rpe_and_full_hr_data_selects_srpe(self) -> None:
        # HR alone would be applicable (150 bpm, thresholds configured);
        # the RPE must win anyway for strength sports.
        activity = ActivityLoadInput(
            sport="WeightTraining",
            duration_s=3600.0,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "srpe"
        assert selection.tss == pytest.approx(210.0)  # 7 x 60 min x 0.5

    def test_strength_with_rpe_and_no_hr_selects_srpe(self) -> None:
        activity = ActivityLoadInput(
            sport="StrengthWorkout",
            duration_s=3600.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "srpe"
        assert selection.tss == pytest.approx(210.0)

    def test_srpe_bypass_is_traceable_in_skipped(self) -> None:
        """The HR step the strength session bypassed (and the inapplicable
        power/pace methods) appear in ``skipped`` with a reason naming the
        bypass, so the choice is diagnosable and persistable."""
        activity = ActivityLoadInput(
            sport="WeightTraining",
            duration_s=3600.0,
            hr_avg_bpm=95.0,  # real gym-level HR: applicable but bypassed
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "srpe"
        assert "bypass" in selection.skipped["hr"]
        assert "power" in selection.skipped

    def test_strength_without_rpe_still_falls_through_to_hr(self) -> None:
        # No RPE: the existing order applies and HR (when available) wins.
        activity = ActivityLoadInput(
            sport="WeightTraining",
            duration_s=3600.0,
            hr_avg_bpm=150.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert selection.tss == pytest.approx(60.4576391242)

    def test_non_strength_sport_with_rpe_still_prefers_hr(self) -> None:
        # The inversion is for strength ONLY: a ride with HR and an RPE
        # keeps the generic order (hr beats srpe).
        activity = ActivityLoadInput(
            sport="Ride",
            duration_s=3600.0,
            hr_avg_bpm=150.0,
            rpe=7.0,
        )
        selection = select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
        assert selection.method == "hr"
        assert selection.tss == pytest.approx(60.4576391242)

    def test_strength_with_neither_rpe_nor_hr_is_undecidable(self) -> None:
        activity = ActivityLoadInput(sport="Workout", duration_s=3600.0)
        with pytest.raises(ValueError, match="no applicable load method"):
            select_load_method(activity, FULL_THRESHOLDS, coefficients=COEFF)
