"""Reference-value tests for pace/speed-based load: run rTSS and swim sTSS.

Every expected value below is hand-derived from the formulas documented in
``app/engine/load.py``.

Run rTSS (Minetti et al. 2002 grade model)
------------------------------------------

Minetti energy cost of running per unit distance (J kg^-1 m^-1) at grade i:

    Cr(i) = 155.4 i^5 - 30.4 i^4 - 43.3 i^3 + 46.3 i^2 + 19.5 i + 3.6

Hand arithmetic for the reference grades:

    Cr(0)     = 3.6  (flat constant term)
    Cr(0.1):  155.4e-5 = 0.001554;  -30.4e-4 = -0.00304
              -43.3e-3 = -0.0433;     46.3e-2 = 0.463
              19.5*0.1 = 1.95
              3.6 + 1.95 + 0.463 - 0.0433 - 0.00304 + 0.001554
                       = 5.968214
    Cr(0.05): 46.3*0.0025 = 0.11575; -43.3*0.000125 = -0.0054125
              -30.4*6.25e-6 = -0.00019; 155.4*3.125e-7 = 0.0000485625
              19.5*0.05 = 0.975
              3.6 + 0.975 + 0.11575 - 0.0054125 - 0.00019 + 0.0000485625
                       = 4.6851960625
    Cr(-0.05): 46.3*0.0025 = 0.11575; -43.3*(-0.000125) = +0.0054125
               -30.4*6.25e-6 = -0.00019; 155.4*(-3.125e-7) = -0.0000485625
               19.5*(-0.05) = -0.975
               3.6 - 0.975 + 0.11575 + 0.0054125 - 0.00019 - 0.0000485625
                        = 2.7459239375

Grade-adjusted speed: v_flat = v * Cr(i) / Cr(0) = v * Cr(i) / 3.6.

    v = 3.0 m/s at +10%: 3.0 * 5.968214 / 3.6 = 17.904642 / 3.6
                                       = 4.97351166666... m/s
    v = 3.0 m/s at -5%:  3.0 * 2.7459239375 / 3.6 = 8.2377718125 / 3.6
                                       = 2.28826994791... m/s

Stream semantics: the grade of sample k is (alt[k] - alt[k-1]) /
(dist[k] - dist[k-1]); it is 0 when unavailable (k = 0, missing samples,
non-positive distance delta, or absent streams). Normalized graded speed
(NGS) is the mean over valid (non-None) speed samples of v * Cr(i)/3.6.

    Uphill stream (speeds [3.0]*4, dist [0,10,20,30], alt [0,1,2,3]):
    grades k0 = 0 (unavailable), k1..k3 = 0.1.
        NGS = (3.0 + 3 * 4.97351166666) / 4 = 17.920535 / 4
            = 4.48013375 m/s
        IF  = 4.48013375 / 3.5 = 1.28003821428...
        rTSS(1 h) = 1 * 1.28003821428^2 * 100 = 163.84978300...

    Downhill stream (speeds [3.0]*4, dist [0,10,20,30], alt [0,-0.5,-1,-1.5]):
    grades k0 = 0 (unavailable), k1..k3 = -0.05.
        NGS = (3.0 + 3 * 2.28826994791) / 4 = 9.86480984375 / 4
            = 2.46620246093... m/s
        IF  = 2.46620246093 / 3.5 = 0.70462927455...
        rTSS(1 h) = 1 * 0.70462927455^2 * 100 = 49.65024145...

    Mixed-grade series (speeds [3.0]*5, dist [0,10,20,30,40],
    alt [0, 1, 2, 2.5, 2.5]): grades k0 = 0 (unavailable), k1 = 0.1,
    k2 = 0.1, k3 = 0.5/10 = 0.05, k4 = 0/10 = 0.
        NGS = (3.0 + 4.97351166666 + 4.97351166666
                   + 3.0 * 4.6851960625/3.6 + 3.0) / 5
            = (3.0 + 4.97351166666 + 4.97351166666 + 3.90433005208 + 3.0)/5
            = 19.85135338541 / 5 = 3.97027067708 m/s
        IF  = 3.97027067708 / 3.5 = 1.13436305059...
        rTSS(1 h) = 1 * 1.13436305059^2 * 100 = 128.67795305...

    Missing first speed sample on a +10% climb (speeds [None, 3.0, 3.0, 3.0],
    dist [0,10,20,30], alt [0,1,2,3]): the missing sample is excluded, the
    remaining three all sit at grade 0.1:
        NGS = 4.97351166666 m/s
        IF  = 4.97351166666 / 3.5 = 1.42100333333...
        rTSS(1 h) = 1 * 1.42100333333^2 * 100 = 201.92504733...

    No altitude stream at all: every grade is 0, so NGS = mean speed.
        v = 3.0, thr = 3.5: IF = 6/7, rTSS(1 h) = (6/7)^2 * 100
                          = 36/49 * 100 = 73.4693877551...

    Gap inside the altitude stream (speeds [3.0]*4, dist [0,10,20,30],
    alt [None, 1, 2, 3]): grades k0 = 0 (valid sample, grade unavailable),
    k1 = 0 (alt[1] - alt[0] unavailable), k2 = k3 = 0.1.
        NGS = (3.0 + 3.0 + 2 * 4.97351166666) / 4 = 15.94702333 / 4
            = 3.98675583333 m/s

    Zero distance delta (GPS noise; speeds [3.0]*4, dist [0,10,10,20],
    alt [0,1,1,2]): grades k1 = 0.1, k2 = 0 (delta_d = 0), k3 = 0.1.
        NGS = (4.97351166666 + 3.0 + 4.97351166666 + 3.0) / 4
            = 15.94702333 / 4 = 3.98675583333 m/s

rTSS identity (Coggan-style normalisation to threshold):

    IF = NGS / threshold_run_speed
    rTSS = duration_h * IF^2 * 100

Flat constant-speed run at threshold speed, 600 s:
    IF = 3.5 / 3.5 = 1, rTSS = (600/3600) * 1 * 100 = 16.66666666...

Swim sTSS
---------

Normalized swim speed: the arithmetic mean of valid, strictly positive
speed samples. Rest (zero-speed), negative and missing samples are
excluded: rest time is recovery, not locomotion, and its load contribution
is already carried by the wall-clock duration term; excluding it keeps the
intensity factor representative of actual swimming.

    Mixed series (speeds [1.2, 0.9, 0.9, 1.2, 0.0, None]):
        valid positive speeds: 1.2, 0.9, 0.9, 1.2
        NSS = 4.2 / 4 = 1.05 m/s
        IF  = 1.05 / 0.7 = 1.5
        sTSS(1 h) = 1 * 1.5^3 * 100 = 337.5

Constant-speed swim at CSS, 600 s:
    IF = 0.8333 / 0.8333 = 1, sTSS = (600/3600) * 1 * 100 = 16.66666666...
"""

from collections.abc import Sequence
from typing import Final

import pytest

from app.engine.load import (
    RunPaceLoad,
    SwimPaceLoad,
    grade_adjusted_speed,
    minetti_energy_cost,
    normalized_graded_speed,
    normalized_swim_speed,
    run_pace_load,
    run_pace_tss,
    swim_pace_load,
    swim_tss,
)

THRESHOLD_RUN_SPEED: Final = 3.5  # m/s (placeholder until owner sets one)
CSS: Final = 0.7  # m/s


class TestMinettiEnergyCost:
    def test_flat_reference_value(self) -> None:
        # Cr(0) = 3.6 J/kg/m: the polynomial's constant term.
        assert minetti_energy_cost(0.0) == 3.6

    def test_uphill_reference_value(self) -> None:
        # Hand arithmetic in module docstring: Cr(0.1) = 5.968214.
        assert minetti_energy_cost(0.1) == pytest.approx(5.968214, rel=1e-12)

    def test_gentle_uphill_reference_value(self) -> None:
        # Hand arithmetic in module docstring: Cr(0.05) = 4.6851960625.
        assert minetti_energy_cost(0.05) == pytest.approx(4.6851960625, rel=1e-12)

    def test_downhill_reference_value(self) -> None:
        # Hand arithmetic in module docstring: Cr(-0.05) = 2.7459239375.
        assert minetti_energy_cost(-0.05) == pytest.approx(
            2.7459239375, rel=1e-12
        )


class TestGradeAdjustedSpeed:
    def test_uphill_reference_value(self) -> None:
        # 3.0 * Cr(0.1)/3.6 = 3.0 * 5.968214 / 3.6 = 4.97351166666... m/s.
        assert grade_adjusted_speed(3.0, 0.1) == pytest.approx(
            4.973511666666667, rel=1e-12
        )

    def test_downhill_reference_value(self) -> None:
        # 3.0 * Cr(-0.05)/3.6 = 3.0 * 2.7459239375 / 3.6 = 2.28826994791... m/s.
        assert grade_adjusted_speed(3.0, -0.05) == pytest.approx(
            2.2882699479166665, rel=1e-12
        )

    def test_flat_grade_is_identity(self) -> None:
        assert grade_adjusted_speed(3.0, 0.0) == 3.0


def _run_stream(
    speeds: Sequence[float | None],
    distances: Sequence[float | None],
    altitudes: Sequence[float | None],
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    return list(speeds), list(distances), list(altitudes)


class TestNormalizedGradedSpeed:
    def test_flat_run_ngs_equals_mean_speed(self) -> None:
        # Constant speed on flat ground (all altitude deltas 0): every
        # grade is 0, so NGS degenerates to the mean speed, 3.5 m/s.
        speeds, dists, alts = _run_stream(
            [3.5] * 600,
            [3.5 * k for k in range(600)],
            [0.0] * 600,
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(3.5, rel=1e-12)

    def test_uphill_stream_reference_value(self) -> None:
        # Hand arithmetic in module docstring: NGS = 4.48013375 m/s.
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0, 30.0], [0.0, 1.0, 2.0, 3.0]
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(4.48013375, rel=1e-12)

    def test_downhill_stream_reference_value(self) -> None:
        # Hand arithmetic in module docstring: NGS = 2.46620246093... m/s.
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0, 30.0], [0.0, -0.5, -1.0, -1.5]
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(2.4662024609374997, rel=1e-12)

    def test_mixed_grade_series_reference_value(self) -> None:
        # Hand arithmetic in module docstring: NGS = 3.97027067708... m/s.
        speeds, dists, alts = _run_stream(
            [3.0] * 5,
            [0.0, 10.0, 20.0, 30.0, 40.0],
            [0.0, 1.0, 2.0, 2.5, 2.5],
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(3.970270677083333, rel=1e-12)

    def test_missing_speed_samples_excluded(self) -> None:
        # Hand arithmetic in module docstring: NGS = 4.97351166666... m/s
        # (the missing first sample is excluded, remaining at grade 0.1).
        speeds, dists, alts = _run_stream(
            [None, 3.0, 3.0, 3.0], [0.0, 10.0, 20.0, 30.0], [0.0, 1.0, 2.0, 3.0]
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(4.973511666666667, rel=1e-12)

    def test_no_altitude_stream_every_grade_is_zero(self) -> None:
        # Grade unavailable (no altitude data at all) -> grade 0, so NGS is
        # exactly the mean speed. Documented semantics, never a guess.
        speeds, dists, _ = _run_stream([3.0] * 4, [0.0, 10.0, 20.0, 30.0], [])
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=None
        ) == pytest.approx(3.0, rel=1e-12)

    def test_no_distance_stream_every_grade_is_zero(self) -> None:
        # Same as above but with the distance stream absent: without
        # horizontal displacement the grade is undefined -> 0.
        speeds, _, alts = _run_stream([3.0] * 4, [], [0.0, 1.0, 2.0, 3.0])
        assert normalized_graded_speed(
            speeds, distance_samples=None, altitude_samples=alts
        ) == pytest.approx(3.0, rel=1e-12)

    def test_gap_inside_altitude_stream_treated_as_grade_zero(self) -> None:
        # Hand arithmetic in module docstring: NGS = 4.31567444444 m/s.
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0, 30.0], [None, 1.0, 2.0, 3.0]
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(3.986755833333333, rel=1e-12)

    def test_zero_distance_delta_treated_as_grade_zero(self) -> None:
        # Hand arithmetic in module docstring: NGS = 3.98675583333 m/s.
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 10.0, 20.0], [0.0, 1.0, 1.0, 2.0]
        )
        assert normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        ) == pytest.approx(3.986755833333333, rel=1e-12)

    def test_no_usable_speed_data_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable"):
            normalized_graded_speed([])
        with pytest.raises(ValueError, match="no usable"):
            normalized_graded_speed([None, None])

    def test_mismatched_stream_lengths_raise(self) -> None:
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0], [0.0, 1.0, 2.0, 3.0]
        )
        with pytest.raises(ValueError, match="length"):
            normalized_graded_speed(
                speeds, distance_samples=dists, altitude_samples=alts
            )


class TestRunPaceTss:
    def test_flat_run_at_threshold_is_exact_identity(self) -> None:
        # Constant speed at threshold: IF = 1 and, per the formula,
        # rTSS = duration_h * IF^2 * 100 = 16.66666666... for 600 s.
        speeds, dists, alts = _run_stream(
            [3.5] * 600,
            [3.5 * k for k in range(600)],
            [0.0] * 600,
        )
        ngs = normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        )
        assert run_pace_tss(600.0, ngs, THRESHOLD_RUN_SPEED) == pytest.approx(
            600.0 / 3600.0 * (ngs / THRESHOLD_RUN_SPEED) ** 2 * 100.0, rel=1e-12
        )
        assert run_pace_tss(600.0, ngs, THRESHOLD_RUN_SPEED) == pytest.approx(
            16.666666666666664, rel=1e-12
        )

    def test_uphill_stream_reference_value(self) -> None:
        # NGS = 4.48013375 (docstring arithmetic); rTSS(1 h) = 163.84978300...
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0, 30.0], [0.0, 1.0, 2.0, 3.0]
        )
        ngs = normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        )
        assert run_pace_tss(3600.0, ngs, THRESHOLD_RUN_SPEED) == pytest.approx(
            163.84978300317596, rel=1e-12
        )

    def test_downhill_stream_reference_value(self) -> None:
        # NGS = 2.46620246093; rTSS(1 h) = 49.65024145...
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0, 30.0], [0.0, -0.5, -1.0, -1.5]
        )
        ngs = normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        )
        assert run_pace_tss(3600.0, ngs, THRESHOLD_RUN_SPEED) == pytest.approx(
            49.65024145578923, rel=1e-12
        )

    def test_missing_sample_reference_value(self) -> None:
        # NGS = 4.97351166666; rTSS(1 h) = 201.92504733...
        speeds, dists, alts = _run_stream(
            [None, 3.0, 3.0, 3.0], [0.0, 10.0, 20.0, 30.0], [0.0, 1.0, 2.0, 3.0]
        )
        ngs = normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=alts
        )
        assert run_pace_tss(3600.0, ngs, THRESHOLD_RUN_SPEED) == pytest.approx(
            201.9250473344444, rel=1e-12
        )

    def test_no_altitude_stream_reference_value(self) -> None:
        # All grades 0: NGS = 3.0, IF = 6/7, rTSS(1 h) = 36/49 * 100
        # = 73.4693877551... (hand arithmetic in module docstring).
        speeds, dists, _ = _run_stream([3.0] * 4, [0.0, 10.0, 20.0, 30.0], [])
        ngs = normalized_graded_speed(
            speeds, distance_samples=dists, altitude_samples=None
        )
        assert run_pace_tss(3600.0, ngs, THRESHOLD_RUN_SPEED) == pytest.approx(
            36.0 / 49.0 * 100.0, rel=1e-12
        )

    def test_non_positive_threshold_raises(self) -> None:
        # Owner has no run threshold pace in Intervals.icu yet: a missing
        # or invalid threshold must raise, never silently default.
        with pytest.raises(ValueError, match="threshold"):
            run_pace_tss(3600.0, 3.0, 0.0)
        with pytest.raises(ValueError, match="threshold"):
            run_pace_tss(3600.0, 3.0, -3.5)

    def test_non_positive_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            run_pace_tss(0.0, 3.0, THRESHOLD_RUN_SPEED)
        with pytest.raises(ValueError, match="duration"):
            run_pace_tss(-1.0, 3.0, THRESHOLD_RUN_SPEED)


class TestRunPaceLoad:
    def test_returns_typed_result(self) -> None:
        # Uphill stream: NGS = 4.48013375, IF = 1.28003821428...,
        # rTSS(1 h) = 163.84978300... (docstring arithmetic).
        speeds, dists, alts = _run_stream(
            [3.0] * 4, [0.0, 10.0, 20.0, 30.0], [0.0, 1.0, 2.0, 3.0]
        )
        result = run_pace_load(
            speeds,
            distance_samples=dists,
            altitude_samples=alts,
            duration_s=3600.0,
            threshold_run_speed=THRESHOLD_RUN_SPEED,
        )
        assert isinstance(result, RunPaceLoad)
        assert result.normalized_graded_speed == pytest.approx(
            4.48013375, rel=1e-12
        )
        assert result.intensity_factor == pytest.approx(
            4.48013375 / 3.5, rel=1e-12
        )
        assert result.tss == pytest.approx(163.84978300317596, rel=1e-12)

    def test_no_usable_data_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable"):
            run_pace_load(
                [None],
                duration_s=3600.0,
                threshold_run_speed=THRESHOLD_RUN_SPEED,
            )


class TestNormalizedSwimSpeed:
    def test_constant_speed_reference_value(self) -> None:
        assert normalized_swim_speed([0.8333] * 600) == pytest.approx(
            0.8333, rel=1e-12
        )

    def test_rest_and_missing_samples_excluded(self) -> None:
        # Valid positive speeds 1.2, 0.9, 0.9, 1.2 -> mean 4.2/4 = 1.05.
        # Zero (rest) and None samples are excluded from the mean; the
        # rationale is documented in the module docstring.
        speeds: list[float | None] = [1.2, 0.9, 0.9, 1.2, 0.0, None]
        assert normalized_swim_speed(speeds) == pytest.approx(1.05, rel=1e-12)

    def test_no_usable_swim_data_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable"):
            normalized_swim_speed([])
        with pytest.raises(ValueError, match="no usable"):
            normalized_swim_speed([None])
        with pytest.raises(ValueError, match="no usable"):
            normalized_swim_speed([0.0, 0.0, 0.0])


class TestSwimTss:
    def test_constant_speed_at_css_exact_identity(self) -> None:
        # Constant speed at CSS: IF = 1 and sTSS = duration_h * IF^3 * 100
        # = 16.66666666... for 600 s.
        assert swim_tss(600.0, 0.8333, 0.8333) == pytest.approx(
            600.0 / 3600.0 * 1.0**3 * 100.0, rel=1e-12
        )
        assert swim_tss(600.0, 0.8333, 0.8333) == pytest.approx(
            16.666666666666664, rel=1e-12
        )

    def test_mixed_series_reference_value(self) -> None:
        # NSS = 1.05 (docstring arithmetic), CSS = 0.7:
        # IF = 1.5, sTSS(1 h) = 1 * 1.5^3 * 100 = 337.5.
        speeds: list[float | None] = [1.2, 0.9, 0.9, 1.2, 0.0, None]
        nss = normalized_swim_speed(speeds)
        assert swim_tss(3600.0, nss, 0.7) == pytest.approx(337.5, rel=1e-12)

    def test_non_positive_css_raises(self) -> None:
        with pytest.raises(ValueError, match="CSS"):
            swim_tss(3600.0, 1.0, 0.0)
        with pytest.raises(ValueError, match="CSS"):
            swim_tss(3600.0, 1.0, -0.7)

    def test_non_positive_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            swim_tss(0.0, 1.0, 0.7)


class TestSwimPaceLoad:
    def test_returns_typed_result(self) -> None:
        # Mixed series: NSS = 1.05, IF = 1.5, sTSS(1 h) = 337.5.
        speeds: list[float | None] = [1.2, 0.9, 0.9, 1.2, 0.0, None]
        result = swim_pace_load(speeds, duration_s=3600.0, css=0.7)
        assert isinstance(result, SwimPaceLoad)
        assert result.normalized_swim_speed == pytest.approx(1.05, rel=1e-12)
        assert result.intensity_factor == pytest.approx(1.5, rel=1e-12)
        assert result.tss == pytest.approx(337.5, rel=1e-12)

    def test_no_usable_swim_data_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable"):
            swim_pace_load([0.0], duration_s=3600.0, css=0.7)
