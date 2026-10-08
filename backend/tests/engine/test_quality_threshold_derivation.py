"""Robust run-threshold derivation (data quality, pure engine).

Established against the parent's real-data measurements: the MAX-of-bests
statistic is the most outlier-sensitive one possible, and on the owner's
real history it produced a best 5-minute effort of 3:01.5/km while the
median pace inside the threshold HR band (3:41.9/km) came out FASTER than
the best 20-minute effort (4:17.1/km) — physiologically contradictory.
The derivation therefore aggregates per-run best efforts with the MEDIAN
across runs (a few corrupt runs cannot dominate), and never returns a
fabricated single number when the independent evidence disagrees: the
outcome is an explicit ``candidate`` / ``insufficient_data`` /
``contradictory_data`` with the disagreement defined numerically
(:data:`app.engine.quality.DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE`, 10%).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.engine.quality import (
    DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE,
    CleanedRun,
    derive_run_threshold_candidate,
)

# Friel Z4-top..Z5a for the owner's documented run LTHR of 169 bpm:
# 0.95 x 169 = 160.55, 1.02 x 169 = 172.38 (band bounds, inclusive halves
# resolved by the project's strict convention inside the engine).
BAND = (160.55, 172.38)


def run(
    chunks: Sequence[tuple[int, float, float]],
    *,
    with_hr: bool = True,
) -> CleanedRun:
    """One synthetic run from (count, speed_mps, hr_bpm) chunks."""
    speed: list[float | None] = []
    hr: list[float | None] = []
    for count, speed_value, hr_value in chunks:
        speed.extend([speed_value] * count)
        if with_hr:
            hr.extend([hr_value] * count)
    return CleanedRun(speed_mps=speed, hr_bpm=hr if with_hr else None)


def steady_run(pace: float, *, hr: float = 168.0, n: int = 1800) -> CleanedRun:
    """A constant-pace run: every best-effort duration equals ``pace``."""
    return run([(n, pace, hr)])


class TestCandidateOutcome:
    """Consistent evidence yields a candidate with its full trail."""

    def test_consistent_history_yields_candidate(self) -> None:
        runs = [steady_run(3.0) for _ in range(4)]
        result = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)

        assert result.outcome == "candidate"
        assert result.threshold_pace_mps == pytest.approx(3.0)
        assert result.threshold_pace_sec_per_km == pytest.approx(1000.0 / 3.0)
        assert result.best_efforts_mps[300] == pytest.approx(3.0)
        assert result.best_efforts_mps[1200] == pytest.approx(3.0)
        assert result.threshold_hr_pace_mps == pytest.approx(3.0)
        assert result.threshold_hr_sample_count == 4 * 1800
        assert result.agreement_relative_deviation == pytest.approx(0.0)
        assert result.n_runs_input == 4
        assert result.n_runs_used == 4

    def test_cs_fit_used_as_anchor_when_available(self) -> None:
        # Fast 600 s block at 3.2 m/s then steady 1200 s at 2.9 m/s: the
        # robust curve has distinct points, so the CS fit exists and anchors.
        runs = [
            run([(600, 3.2, 170.0), (1200, 2.9, 140.0)]) for _ in range(4)
        ]
        result = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)

        assert result.outcome == "candidate"
        assert result.cs_fit is not None
        assert 0.0 < result.cs_fit.cs_mps < 3.2
        assert result.threshold_pace_mps == pytest.approx(result.cs_fit.cs_mps)
        assert result.threshold_hr_pace_mps == pytest.approx(3.2)
        assert result.agreement_relative_deviation is not None
        assert result.agreement_relative_deviation <= (
            DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE
        )

    def test_candidate_without_hr_evidence_reports_no_agreement(self) -> None:
        runs = [run([(1800, 3.0, 168.0)], with_hr=False) for _ in range(3)]
        result = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)

        assert result.outcome == "candidate"
        assert result.threshold_pace_mps == pytest.approx(3.0)
        assert result.threshold_hr_pace_mps is None
        assert result.threshold_hr_sample_count == 0
        assert result.agreement_relative_deviation is None

    def test_min_runs_is_configurable(self) -> None:
        runs = [steady_run(3.0) for _ in range(2)]

        insufficient = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)
        assert insufficient.outcome == "insufficient_data"

        allowed = derive_run_threshold_candidate(
            runs, threshold_hr_band_bpm=BAND, min_runs=2
        )
        assert allowed.outcome == "candidate"


class TestRobustAgainstCorruptRuns:
    """The parent's core complaint: ONE corrupt run must not dominate."""

    def test_median_not_max_across_runs(self) -> None:
        clean = [steady_run(3.0) for _ in range(5)]
        corrupt = steady_run(9.0)  # one poisoned run, everything at 9 m/s
        result = derive_run_threshold_candidate(
            [*clean, corrupt], threshold_hr_band_bpm=BAND
        )

        # The MEDIAN across runs keeps the clean evidence; the max-of-bests
        # statistic the derivation replaced would have reported 9.0.
        assert result.best_efforts_mps[300] == pytest.approx(3.0)
        assert result.threshold_pace_mps == pytest.approx(3.0)

    def test_hr_median_robust_to_one_corrupt_run(self) -> None:
        clean = [steady_run(3.0) for _ in range(5)]
        corrupt = steady_run(9.0)
        result = derive_run_threshold_candidate(
            [*clean, corrupt], threshold_hr_band_bpm=BAND
        )

        assert result.threshold_hr_pace_mps == pytest.approx(3.0)


class TestContradictoryOutcome:
    """Disagreeing evidence never becomes a fabricated single number."""

    def make_contradictory(self) -> list[CleanedRun]:
        # Short sprint bursts at 6.0 m/s tagged with in-band HR inside an
        # otherwise slow run: the HR-band MEDIAN (6.0) comes out far faster
        # than the robust best-effort anchor (~3.3) — physiologically
        # contradictory, exactly the parent's real-data finding.
        return [
            run([(300, 6.0, 168.0), (1500, 2.5, 140.0)]) for _ in range(4)
        ]

    def test_disagreement_yields_contradictory_data(self) -> None:
        result = derive_run_threshold_candidate(
            self.make_contradictory(), threshold_hr_band_bpm=BAND
        )

        assert result.outcome == "contradictory_data"
        assert result.threshold_pace_mps is None
        assert result.anchor_mps is not None
        assert result.threshold_hr_pace_mps is not None
        assert result.agreement_relative_deviation is not None
        assert result.agreement_relative_deviation > (
            DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE
        )
        assert "faster" in result.detail

    def test_disagreement_tolerance_is_configurable(self) -> None:
        wide = derive_run_threshold_candidate(
            self.make_contradictory(),
            threshold_hr_band_bpm=BAND,
            agreement_tolerance=1.0,
        )

        # With a tolerance of 100% the same evidence is inside the band and
        # the outcome becomes a candidate (the rule is documented, not magic).
        assert wide.outcome == "candidate"


class TestInsufficientData:
    """Missing / too-sparse history is reported, never guessed."""

    def test_no_runs(self) -> None:
        result = derive_run_threshold_candidate([], threshold_hr_band_bpm=BAND)

        assert result.outcome == "insufficient_data"
        assert result.threshold_pace_mps is None

    def test_fewer_than_min_runs(self) -> None:
        result = derive_run_threshold_candidate(
            [steady_run(3.0) for _ in range(2)], threshold_hr_band_bpm=BAND
        )

        assert result.outcome == "insufficient_data"
        assert "2" in result.detail

    def test_unusable_speed_streams(self) -> None:
        runs = [
            CleanedRun(speed_mps=[None] * 1800, hr_bpm=[168.0] * 1800)
            for _ in range(4)
        ]
        result = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)

        assert result.outcome == "insufficient_data"

    def test_hr_evidence_alone_is_never_a_candidate(self) -> None:
        """Runs with no measurable best effort contribute no anchor even when
        their HR-band pace exists — best-effort evidence is required."""
        runs = [
            CleanedRun(speed_mps=[None] * 100, hr_bpm=[168.0] * 100)
            for _ in range(4)
        ]
        result = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)

        assert result.outcome == "insufficient_data"


class TestHrBandConvention:
    """Band membership follows the strict boundary convention."""

    def test_exactly_at_band_edge_is_outside(self) -> None:
        # Every sample sits EXACTLY at the band's lower edge (160.55 bpm):
        # exactly at a limit counts as beyond it, so no sample is in-band.
        runs = [steady_run(3.0, hr=160.55) for _ in range(3)]
        result = derive_run_threshold_candidate(runs, threshold_hr_band_bpm=BAND)

        assert result.outcome == "candidate"
        assert result.threshold_hr_pace_mps is None
        assert result.threshold_hr_sample_count == 0

    def test_missing_hr_stream_per_run_is_skipped(self) -> None:
        # CORRECTION (2026-10, finishing the failed attempt): the original
        # call supplied only 2 runs against the default min_runs=3 (pinned
        # by test_min_runs_is_configurable below), so it could never have
        # passed GREEN. min_runs=2 is passed explicitly; the INTENT (runs
        # without an HR stream contribute no HR evidence but still count
        # toward the run count) is unchanged.
        runs = [steady_run(3.0), run([(1800, 3.0, 168.0)], with_hr=False)]
        result = derive_run_threshold_candidate(
            runs, threshold_hr_band_bpm=BAND, min_runs=2
        )

        assert result.outcome == "candidate"
        assert result.threshold_hr_pace_mps == pytest.approx(3.0)
        assert result.threshold_hr_sample_count == 1800


class TestValidation:
    """Documented errors for nonsensical parameters (engine convention)."""

    def make_runs(self) -> list[CleanedRun]:
        return [steady_run(3.0) for _ in range(3)]

    def test_min_runs_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="min_runs"):
            derive_run_threshold_candidate(
                self.make_runs(), threshold_hr_band_bpm=BAND, min_runs=0
            )

    def test_tolerance_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="agreement_tolerance"):
            derive_run_threshold_candidate(
                self.make_runs(), threshold_hr_band_bpm=BAND, agreement_tolerance=0.0
            )

    def test_band_must_be_ascending(self) -> None:
        with pytest.raises(ValueError, match="band"):
            derive_run_threshold_candidate(
                self.make_runs(), threshold_hr_band_bpm=(172.0, 160.0)
            )

    def test_durations_must_not_be_empty(self) -> None:
        with pytest.raises(ValueError, match="durations"):
            derive_run_threshold_candidate(
                self.make_runs(), threshold_hr_band_bpm=BAND, durations_s=[]
            )

    def test_hr_stream_must_align_with_speed_stream(self) -> None:
        misaligned = CleanedRun(speed_mps=[3.0] * 100, hr_bpm=[168.0] * 99)
        with pytest.raises(ValueError, match="align"):
            derive_run_threshold_candidate(
                [misaligned for _ in range(3)], threshold_hr_band_bpm=BAND
            )
