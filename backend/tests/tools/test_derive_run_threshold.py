"""Deterministic tests for the read-only threshold-derivation CLI.

The CLI (:mod:`app.tools.derive_run_threshold`) runs
:func:`app.engine.quality.derive_run_threshold_candidate` over the STORED
history and prints the typed candidate, the rejected activities with their
machine-readable reasons and the final confidence/contradiction outcome.

READ-ONLY contract: the tool only SELECTs — it never writes to the
database and never touches Intervals.icu. The DB-backed path is exercised
by a manual run against the dev database (an evidence step, not a test,
exactly like the PMC cross-check's real-data run); the deterministic tests
here cover the pure helpers (threshold-HR band derivation from LTHR, pace
formatting, report formatting) so the report shape cannot drift silently.
"""

from __future__ import annotations

import pytest

from app.engine.quality import (
    DEFAULT_PLAUSIBILITY_BANDS_MPS,
    ActivityPlausibility,
    RunThresholdCandidate,
)
from app.tools.derive_run_threshold import (
    RUN_LTHR_BAND_CEILING_PCT,
    RUN_LTHR_BAND_FLOOR_PCT,
    format_pace,
    format_report,
    threshold_hr_band_from_lthr,
)


class TestThresholdHrBand:
    """The band is derived from the owner's LTHR, never hardcoded."""

    def test_band_is_friel_z4_bottom_to_z5a_top(self) -> None:
        # Friel run HR zones: Z4 starts at 95% LTHR, Z5a ends at 102% LTHR.
        # For the owner's documented LTHR of 169 bpm: 160.55 .. 172.38.
        floor, ceiling = threshold_hr_band_from_lthr(169.0)
        assert floor == pytest.approx(0.95 * 169.0)
        assert ceiling == pytest.approx(1.02 * 169.0)

    def test_band_percentages_are_the_documented_constants(self) -> None:
        assert RUN_LTHR_BAND_FLOOR_PCT == 0.95
        assert RUN_LTHR_BAND_CEILING_PCT == 1.02

    def test_nonpositive_lthr_rejected(self) -> None:
        with pytest.raises(ValueError, match="LTHR"):
            threshold_hr_band_from_lthr(0.0)


class TestFormatPace:
    """Pace formatting: m/s -> min/km with one decimal, never invented."""

    def test_threshold_like_pace(self) -> None:
        # 3.8900 m/s = 257.07 s/km = 4:17.1/km (the parent's best-20-min value).
        assert format_pace(3.8900) == "4:17.1/km"

    def test_easy_pace(self) -> None:
        # 2.5 m/s = 400 s/km = 6:40.0/km.
        assert format_pace(2.5) == "6:40.0/km"

    def test_missing_pace_is_reported_not_guessed(self) -> None:
        assert format_pace(None) == "n/a"


def _candidate(**overrides: object) -> RunThresholdCandidate:
    """A minimal candidate-shaped result for report-formatting tests."""
    fields: dict[str, object] = {
        "outcome": "candidate",
        "threshold_pace_mps": 3.0,
        "threshold_pace_sec_per_km": 1000.0 / 3.0,
        "best_efforts_mps": {300: 3.0, 1200: 3.0},
        "cs_fit": None,
        "anchor_mps": 3.0,
        "threshold_hr_pace_mps": 3.0,
        "threshold_hr_sample_count": 7200,
        "agreement_relative_deviation": 0.0,
        "n_runs_input": 4,
        "n_runs_used": 4,
        "detail": "anchor and HR pace agree",
    }
    fields.update(overrides)
    return RunThresholdCandidate(**fields)  # type: ignore[arg-type]


def _rejected(
    *,
    reason: str = "average_speed_above_ceiling",
    average_speed_mps: float | None = 7.8616,
) -> ActivityPlausibility:
    return ActivityPlausibility(
        outcome="implausible",
        reason=reason,
        average_speed_mps=average_speed_mps,
        detail="average speed 7.862 m/s is at or above the run ceiling "
        f"{DEFAULT_PLAUSIBILITY_BANDS_MPS['run'][1]!r} m/s",
    )


class TestFormatReport:
    """The report shows the candidate, the rejections and the outcome."""

    def test_candidate_report_contains_evidence_and_verdict(self) -> None:
        report = format_report(
            band=(160.55, 172.38),
            n_activities_considered=539,
            n_plausible=530,
            rejected=[("i163428838", "2023-05-01", "Lunch Run", _rejected())],
            not_assessable=[("i1", "2020-01-01", "Old Run", "missing_distance_or_duration")],
            n_skipped_no_stream=2,
            n_samples_capped_total=1234,
            candidate=_candidate(),
        )

        assert "candidate" in report
        assert "READ-ONLY" in report
        assert "160.55" in report and "172.38" in report
        assert "i163428838" in report
        assert "average_speed_above_ceiling" in report
        assert "missing_distance_or_duration" in report
        assert "7.862 m/s" in report
        assert "1234" in report
        assert "5:33.3/km" in report  # 3.0 m/s threshold pace

    def test_contradictory_report_shows_both_estimates_no_pace(self) -> None:
        report = format_report(
            band=(160.55, 172.38),
            n_activities_considered=539,
            n_plausible=530,
            rejected=[],
            not_assessable=[],
            n_skipped_no_stream=0,
            n_samples_capped_total=0,
            candidate=_candidate(
                outcome="contradictory_data",
                threshold_pace_mps=None,
                threshold_pace_sec_per_km=None,
                anchor_mps=2.7473,
                threshold_hr_pace_mps=6.0,
                agreement_relative_deviation=0.5421,
                detail="evidence disagrees: threshold-HR pace is faster",
            ),
        )

        assert "contradictory_data" in report
        assert "2.747" in report
        assert "6.000" in report
        assert "faster" in report

    def test_insufficient_report_names_the_reason(self) -> None:
        report = format_report(
            band=(160.55, 172.38),
            n_activities_considered=10,
            n_plausible=10,
            rejected=[],
            not_assessable=[],
            n_skipped_no_stream=0,
            n_samples_capped_total=0,
            candidate=_candidate(
                outcome="insufficient_data",
                threshold_pace_mps=None,
                threshold_pace_sec_per_km=None,
                anchor_mps=None,
                threshold_hr_pace_mps=None,
                agreement_relative_deviation=None,
                detail="only 2 of 10 runs produced usable best-effort evidence",
            ),
        )

        assert "insufficient_data" in report
        assert "only 2 of 10 runs" in report
