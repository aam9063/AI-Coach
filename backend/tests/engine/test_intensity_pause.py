"""Pause-aware time-in-zone weighting (RID-5, section 7.5 — pause rule).

Real-data quality finding (2026-07 dev evidence): ``weekly_intensity``
seconds equalled the SUM OF THE STREAM SPANS, not ``moving_time`` — the
week-24 rides carried spans of 7987 s + 16448 s = 24435 s against only
20910 s of ``moving_time`` (span/moving ratios 1.12-1.29). A pause shows up
in the stream with the athlete stopped and the HR drifting down, so the
interval-weighted classification counted every stopped second as easy-zone
(Z1) time: stops systematically inflated Z1 and the reported "time in zone"
overstated training time.

The rule under test (:func:`app.engine.intensity.moving_weights`) is the
pure weighting core the intensity service consumes (see
``tests/db/test_intensity_persistence.py::TestPauseTimeExclusion`` for the
service-level round trip):

1. SPEED RULE: a sample whose speed stream value exists and is tolerantly
   at or below the stopped-speed tolerance is NON-MOVING — it contributes
   no time, whatever its zone. Tolerance default
   :data:`app.engine.intensity.DEFAULT_PAUSE_SPEED_TOLERANCE_MPS`
   (the Garmin FIT SDK's documented default ``stopped_speed_threshold`` of
   0.1 m/s), configurable.
2. GAP-CAP RULE (the general FALLBACK when no speed stream exists): with a
   ``time`` stream, the interval a sample inherits is compared against a
   cap of ``gap_cap_median_multiple`` x the MEDIAN POSITIVE sample interval
   (median over the strictly positive deltas only, so zero-width samples
   cannot collapse the cap to zero and exclude everything). An interval at
   or beyond the cap is a pause/recording gap: the sample's zone must not
   inherit it, so it contributes nothing.
3. No ``time`` stream: each valid sample counts exactly one second (the
   documented per-second convention) — the speed rule still applies, the
   gap cap cannot (no timestamps to measure a gap on). No speed stream AND
   no time stream: every sample counts one second (nothing detectable —
   documented limitation).

Boundary convention (the project's stated strict/tolerant rule, same
precedent as the durability decoupling band — "exactly at the limit counts
as beyond the favorable side"): a speed EXACTLY at the tolerance counts as
stopped, and an interval EXACTLY at the cap counts as a pause; both
compared tolerantly (``_same_boundary``-style isclose) so float noise
cannot flip a classification.

Every case here is pure: no DB, no I/O (section 6 purity rule).
"""

import pytest

from app.engine.intensity import (
    DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE,
    DEFAULT_PAUSE_SPEED_TOLERANCE_MPS,
    moving_weights,
)


class TestSpeedRule:
    def test_stopped_samples_contribute_no_time(self) -> None:
        """10 s at 1 Hz, moving 3 m/s except samples 5-7 stopped at 0.0
        m/s: the three stopped seconds contribute nothing (7 s, not 9 s)."""
        times = [float(i) for i in range(10)]
        speeds: list[float | None] = [3.0] * 10
        speeds[5] = speeds[6] = speeds[7] = 0.0
        weights = moving_weights(10, times=times, speeds=speeds)
        assert len(weights) == 10
        assert weights[:5] == [1.0] * 5
        assert weights[5:8] == [0.0, 0.0, 0.0]
        assert weights[8] == pytest.approx(1.0)
        assert weights[9] == pytest.approx(1.0)  # last sample inherits
        assert sum(weights) == pytest.approx(7.0)

    def test_speed_exactly_at_tolerance_is_non_moving(self) -> None:
        """Boundary convention: exactly at the tolerance counts as
        stopped (tolerantly compared — float noise cannot flip it)."""
        times = [0.0, 1.0, 2.0, 3.0]
        tol = DEFAULT_PAUSE_SPEED_TOLERANCE_MPS
        weights = moving_weights(
            4, times=times, speeds=[0.1, tol, tol + 1e-15, 0.1001]
        )
        assert weights[0] == pytest.approx(0.0)
        assert weights[1] == pytest.approx(0.0)
        assert weights[2] == pytest.approx(0.0)  # noise-at-boundary: stopped
        assert weights[3] == pytest.approx(1.0)  # clearly above: moving

    def test_none_speed_sample_is_not_claimed_stopped(self) -> None:
        """A ``None`` speed entry is a GAP, not a zero speed: the sample
        keeps its interval (only a known stopped speed zeroes it)."""
        times = [0.0, 1.0, 2.0, 3.0]
        speeds: list[float | None] = [3.0, None, 3.0, 3.0]
        weights = moving_weights(4, times=times, speeds=speeds)
        assert sum(weights) == pytest.approx(4.0)  # 3 deltas + inherited gap
        assert weights[1] == pytest.approx(1.0)

    def test_all_none_speed_stream_behaves_like_no_speed_stream(self) -> None:
        """A speed stream that is entirely ``None`` gaps cannot back the
        speed rule (the engine's usable-sample rule): nothing is zeroed
        by it and the gap cap remains the fallback."""
        times = [float(i) for i in range(5)]
        speeds: list[float | None] = [None] * 5
        weights = moving_weights(5, times=times, speeds=speeds)
        assert sum(weights) == pytest.approx(5.0)  # deltas only, no zeroing

    def test_no_time_stream_one_second_per_moving_sample(self) -> None:
        """Without a ``time`` stream each valid sample counts one second
        (the per-second convention); the speed rule still applies."""
        weights = moving_weights(4, speeds=[3.0, 0.0, 3.0, 3.0])
        assert weights == [1.0, 0.0, 1.0, 1.0]

    def test_no_speed_stream_no_time_stream_all_one_second(self) -> None:
        """Nothing detectable: every sample counts one second (the
        documented limitation of the per-second convention)."""
        assert moving_weights(3) == [1.0, 1.0, 1.0]


class TestGapCapRule:
    def test_long_gap_without_speed_stream_is_excluded(self) -> None:
        """No speed stream, timestamps 0..4 then a 36 s recording gap then
        40..44: median positive interval 1 s, cap 5 x 1 = 5 s, the 36 s
        gap is a pause — 8 s counted, not 44 s."""
        times = [0.0, 1.0, 2.0, 3.0, 4.0, 40.0, 41.0, 42.0, 43.0, 44.0]
        weights = moving_weights(10, times=times, speeds=None)
        # 4 s before + 4 s after the gap, plus the last sample's inherited
        # (regular) interval — the 36 s pause itself contributes nothing.
        assert sum(weights) == pytest.approx(9.0)
        assert weights[4] == pytest.approx(0.0)  # inherits the 36 s gap
        # ...and the last sample inherits the previous (regular) interval.
        assert weights[9] == pytest.approx(1.0)

    def test_gap_exactly_at_the_cap_is_a_pause(self) -> None:
        """Boundary convention: an interval exactly at the cap counts as
        beyond (a pause) — tolerated isclose comparison."""
        # deltas: 1,1,1,1,5 -> median 1, cap 5; the 5 s interval is AT the
        # cap, so it is a pause; the last sample inherits it and is too.
        times = [0.0, 1.0, 2.0, 3.0, 4.0, 9.0]
        weights = moving_weights(6, times=times)
        assert weights[4] == pytest.approx(0.0)
        assert weights[5] == pytest.approx(0.0)  # inherits the pause gap
        assert sum(weights) == pytest.approx(4.0)

    def test_interval_just_below_the_cap_is_kept(self) -> None:
        """An interval strictly below the cap is regular sampling — kept."""
        times = [0.0, 1.0, 2.0, 3.0, 4.0, 8.5]  # last delta 4.5 < cap 5
        weights = moving_weights(6, times=times)
        assert weights[4] == pytest.approx(4.5)
        assert weights[5] == pytest.approx(4.5)
        assert sum(weights) == pytest.approx(13.0)

    def test_median_uses_positive_deltas_only(self) -> None:
        """Zero-width samples (duplicate timestamps) must not collapse the
        median — the cap is the multiple of the median POSITIVE interval."""
        # deltas: 5, 0, 1, 1 -> positive median 1, cap 5; nothing collapses.
        times = [0.0, 5.0, 5.0, 6.0, 7.0]
        weights = moving_weights(5, times=times)
        assert weights[2] == pytest.approx(1.0)
        assert weights[3] == pytest.approx(1.0)
        assert weights[4] == pytest.approx(1.0)

    def test_cap_multiple_is_configurable(self) -> None:
        """A larger multiple keeps the same gap a regular interval."""
        times = [0.0, 1.0, 2.0, 3.0, 4.0, 40.0, 41.0]
        kept = moving_weights(7, times=times, gap_cap_median_multiple=50.0)
        assert sum(kept) == pytest.approx(42.0)

    def test_no_positive_deltas_means_no_cap_applies(self) -> None:
        """If no interval is positive there is no median and no cap: only
        the zero/negative-delta and speed rules can zero a weight."""
        times = [5.0, 5.0, 5.0]
        weights = moving_weights(3, times=times)
        assert weights == [0.0, 0.0, 0.0]


class TestMovingWeightsValidation:
    """Bad inputs raise ValueError — never silent."""

    def test_negative_n(self) -> None:
        with pytest.raises(ValueError, match="n"):
            moving_weights(-1)

    def test_non_finite_speed_tolerance(self) -> None:
        with pytest.raises(ValueError, match="speed_tolerance_mps"):
            moving_weights(1, speed_tolerance_mps=float("nan"))

    def test_negative_speed_tolerance(self) -> None:
        with pytest.raises(ValueError, match="speed_tolerance_mps"):
            moving_weights(1, speed_tolerance_mps=-0.1)

    def test_non_positive_gap_cap_multiple(self) -> None:
        with pytest.raises(ValueError, match="gap_cap_median_multiple"):
            moving_weights(1, gap_cap_median_multiple=0.0)

    def test_times_shorter_than_n(self) -> None:
        with pytest.raises(ValueError, match="times"):
            moving_weights(3, times=[0.0, 1.0])

    def test_speeds_shorter_than_n(self) -> None:
        with pytest.raises(ValueError, match="speeds"):
            moving_weights(3, speeds=[1.0, 2.0])

    def test_non_finite_time_entry(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            moving_weights(2, times=[0.0, float("inf")])

    def test_non_finite_speed_entry(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            moving_weights(2, times=[0.0, 1.0], speeds=[1.0, float("nan")])

    def test_zero_tolerance_is_strict_zero_speed(self) -> None:
        """tolerance 0.0 is allowed: only exactly-zero speeds stop."""
        weights = moving_weights(
            3,
            times=[0.0, 1.0, 2.0],
            speeds=[0.0, 0.001, 0.0],
            speed_tolerance_mps=0.0,
        )
        assert weights == [0.0, 1.0, 0.0]


def test_documented_defaults() -> None:
    """The defaults are the documented provenance-tagged constants (FIT
    SDK stopped-speed 0.1 m/s; gap cap 5x the median sample interval)."""
    assert pytest.approx(0.1) == DEFAULT_PAUSE_SPEED_TOLERANCE_MPS
    assert pytest.approx(5.0) == DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE
