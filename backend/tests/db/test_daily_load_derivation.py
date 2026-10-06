"""Unit tests for the daily_load stream derivation and CLI helpers.

LOAD-10 (first half): these are the no-DB tests for the pure derivation
logic that turns stored stream payloads into engine inputs (average HR over
non-``None`` samples, with gaps and empty/absent streams yielding ``None``),
plus the settings -> :class:`ThresholdBundle` mapping and the report
formatter used by the ``python -m app.db.daily_load`` CLI.
"""

import datetime as dt
from datetime import UTC, datetime

import pytest

from app.core.settings import Settings
from app.db.daily_load import format_report, thresholds_from_settings
from app.db.models import ActivityRow
from app.engine.load import ThresholdBundle
from app.services.daily_load import (
    COMBINED_SPORT_KEY,
    DailyLoadReport,
    SkippedActivity,
    average_hr_bpm,
    build_activity_load_input,
)


class TestAverageHrBpm:
    def test_plain_samples(self) -> None:
        assert average_hr_bpm([150.0, 160.0]) == pytest.approx(155.0)

    def test_gaps_are_excluded_from_the_mean(self) -> None:
        # None marks a stream gap (ingest parser semantics): excluded, not 0.
        assert average_hr_bpm([150.0, None, 170.0]) == pytest.approx(160.0)

    def test_all_missing_returns_none(self) -> None:
        assert average_hr_bpm([None, None]) is None

    def test_empty_payload_returns_none(self) -> None:
        assert average_hr_bpm([]) is None

    def test_absent_stream_returns_none(self) -> None:
        assert average_hr_bpm(None) is None

    def test_fractional_mean_is_exact(self) -> None:
        assert average_hr_bpm([140.0, 151.0, 162.0]) == pytest.approx(151.0)


class TestBuildActivityLoadInput:
    def test_hr_stream_becomes_average(self) -> None:
        activity = _activity(duration_s=3600)
        derived, reason = build_activity_load_input(
            activity, {"hr": [150.0, None, 170.0]}
        )
        assert reason is None
        assert derived is not None
        assert derived.hr_avg_bpm == pytest.approx(160.0)
        assert derived.duration_s == 3600.0

    def test_missing_duration_is_undecidable_with_reason(self) -> None:
        derived, reason = build_activity_load_input(_activity(duration_s=None), {})
        assert derived is None
        assert reason is not None
        assert "duration" in reason

    def test_non_positive_duration_is_undecidable_with_reason(self) -> None:
        derived, reason = build_activity_load_input(_activity(duration_s=0), {})
        assert derived is None
        assert reason is not None
        assert "duration" in reason


class TestThresholdsFromSettings:
    @pytest.fixture(autouse=True)
    def _hermetic_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remove owner .env values so the test does not depend on them."""
        for key in (
            "ATHLETE_FTP_W",
            "ATHLETE_LTHR_BPM",
            "ATHLETE_HR_MAX_BPM",
            "ATHLETE_HR_REST_BPM",
            "ATHLETE_CSS_SPEED_MPS",
            "ATHLETE_THRESHOLD_RUN_SPEED_MPS",
            "ATHLETE_WEIGHT_KG",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_maps_every_athlete_setting(self) -> None:
        settings = Settings(
            athlete_ftp_w=180.0,
            athlete_lthr_bpm=169.0,
            athlete_hr_max_bpm=186.0,
            athlete_hr_rest_bpm=65.0,
            athlete_css_speed_mps=0.8333,
            athlete_threshold_run_speed_mps=None,
            athlete_weight_kg=75.0,
        )
        bundle = thresholds_from_settings(settings)
        assert bundle == ThresholdBundle(
            ftp_watts=180.0,
            threshold_run_speed_mps=None,
            css_mps=0.8333,
            lthr_bpm=169.0,
            hr_max_bpm=186.0,
            hr_rest_bpm=65.0,
        )

    def test_unconfigured_thresholds_stay_none(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        bundle = thresholds_from_settings(settings)
        assert bundle.ftp_watts is None
        assert bundle.lthr_bpm is None
        assert bundle.hr_max_bpm is None
        assert bundle.hr_rest_bpm is None
        assert bundle.css_mps is None
        assert bundle.threshold_run_speed_mps is None


class TestFormatReport:
    def test_includes_counts_and_skip_reasons(self) -> None:
        report = DailyLoadReport(
            window_start=dt.date(2026, 7, 1),
            window_end=dt.date(2026, 7, 7),
            engine_version="0.1.0",
            activities_considered=2,
            per_day_activities={dt.date(2026, 7, 1): 2},
            per_sport_rows={"ride": 7, COMBINED_SPORT_KEY: 7},
            per_sport_activities={"ride": 2},
            rows_upserted=14,
            skipped=(
                SkippedActivity(
                    activity_id=42, sport="Ride", reason="no applicable load method"
                ),
            ),
        )
        text = format_report(report)
        assert "2026-07-01" in text
        assert "ride" in text
        assert COMBINED_SPORT_KEY in text
        assert "no applicable load method" in text
        assert "0.1.0" in text


def _activity(*, duration_s: int | None) -> ActivityRow:
    """Minimal ActivityRow exposing only what the derivation reads."""
    return ActivityRow(
        source="intervals",
        source_id="i1",
        type="Ride",
        name="Ride",
        start_time=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        duration_s=duration_s,
    )
