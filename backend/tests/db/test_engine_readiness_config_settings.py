"""The settings→engine mapping for readiness/intensity/durability constants
(RID-10, §7/§14).

Contract (ZON-11 pattern of ``app.db.zones_config``, itself following the
LOAD-11 pattern of ``app.db.daily_load``):
``app.db.engine_readiness_config`` is a PURE mapping from the sourced
``engine_*`` Settings fields onto the pure engine's keyword parameters. The
key names are pinned here against the signatures of the documented engine
functions, the defaults are pinned against the engine's documented module
constants (the fallbacks), and the structured-string parsers (threshold
maps, the sport→modality map, the reference bands) validate with clear
``ValueError`` s.

The helper deliberately has NO caller yet: nothing in ``app/`` consumes
readiness, intensity or durability constants today (Feature 6 will wire the
``get_readiness`` and ``get_intensity_distribution`` tools); it exists so
that wiring is a reviewed, tested mapping instead of an ad-hoc read when
Feature 6 lands.
"""

import inspect
from typing import Any, cast

import pytest

from app.core.settings import Settings
from app.db.engine_readiness_config import (
    durability_constants_from_settings,
    intensity_constants_from_settings,
    parse_reference_bands,
    parse_sport_modality,
    parse_threshold_pcts,
    readiness_constants_from_settings,
)
from app.engine.durability import (
    DEFAULT_DECOUPLING_REFERENCE_BAND,
    DEFAULT_MAX_HALF_INTENSITY_DRIFT,
    DEFAULT_MIN_LONG_SESSION_SECONDS,
    DEFAULT_MIN_TREND_SESSIONS,
    DEFAULT_TREND_WINDOW_DAYS,
    durability_trend,
    session_decoupling,
)
from app.engine.intensity import (
    DEFAULT_FIRST_THRESHOLD_PCTS,
    DEFAULT_MIN_PATTERN_WEEK_SECONDS,
    DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE,
    DEFAULT_PAUSE_SPEED_TOLERANCE_MPS,
    DEFAULT_POLARIZED_BANDS,
    DEFAULT_PYRAMIDAL_BANDS,
    DEFAULT_SECOND_THRESHOLD_PCTS,
    SPORT_MODALITY,
    descriptive_pattern_comparison,
    moving_weights,
    three_zone_model,
)
from app.engine.readiness import (
    DEFAULT_HRV_BAND_SD,
    DEFAULT_HRV_BASELINE_DAYS,
    DEFAULT_HRV_MIN_BASELINE_VALID_DAYS,
    DEFAULT_HRV_WINDOW_DAYS,
    DEFAULT_MIN_WINDOW_VALID_FRACTION,
    DEFAULT_RHR_BAND_SD,
    DEFAULT_RHR_BASELINE_DAYS,
    DEFAULT_RHR_MIN_BASELINE_VALID_DAYS,
    DEFAULT_SLEEP_BAND_SD,
    DEFAULT_SLEEP_BASELINE_DAYS,
    DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS,
    DEFAULT_TSB_VERY_NEGATIVE,
    hrv_readiness,
    readiness_assessment,
    resting_hr_readiness,
    sleep_readiness,
)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


class TestReadinessConstantsFromSettings:
    def test_every_key_is_an_accepted_kwarg_of_its_documented_function(
        self,
    ) -> None:
        constants = readiness_constants_from_settings(_settings())
        # The HRV/resting-HR/sleep functions share parameter names
        # (window_days, baseline_days, band_sd, ...), so the mapping keys
        # are namespaced by signal and the pin records the target function
        # and parameter.
        pinned = {
            "hrv_window_days": (hrv_readiness, "window_days"),
            "hrv_baseline_days": (hrv_readiness, "baseline_days"),
            "hrv_band_sd": (hrv_readiness, "band_sd"),
            "hrv_min_baseline_valid_days": (
                hrv_readiness,
                "min_baseline_valid_days",
            ),
            "min_window_valid_fraction": (
                hrv_readiness,
                "min_window_valid_fraction",
            ),
            "rhr_baseline_days": (resting_hr_readiness, "baseline_days"),
            "rhr_band_sd": (resting_hr_readiness, "band_sd"),
            "rhr_min_baseline_valid_days": (
                resting_hr_readiness,
                "min_baseline_valid_days",
            ),
            "sleep_baseline_days": (sleep_readiness, "baseline_days"),
            "sleep_band_sd": (sleep_readiness, "band_sd"),
            "sleep_min_baseline_valid_days": (
                sleep_readiness,
                "min_baseline_valid_days",
            ),
            "tsb_very_negative_below": (
                readiness_assessment,
                "tsb_very_negative_below",
            ),
        }
        assert set(constants) == set(pinned), (
            "the mapping keys and the pinned keys diverged; update both "
            "together so every constant stays wired to its consumer"
        )
        for key, (func, param) in pinned.items():
            signature = inspect.signature(cast(Any, func))
            assert param in signature.parameters, (
                f"{key!r} maps to {param!r}, which is not a keyword "
                f"parameter of {func.__name__}"
            )

    def test_defaults_match_the_engine_fallbacks(self) -> None:
        constants = readiness_constants_from_settings(_settings())
        assert constants["hrv_window_days"] == DEFAULT_HRV_WINDOW_DAYS
        assert constants["hrv_baseline_days"] == DEFAULT_HRV_BASELINE_DAYS
        assert constants["hrv_band_sd"] == DEFAULT_HRV_BAND_SD
        assert (
            constants["hrv_min_baseline_valid_days"]
            == DEFAULT_HRV_MIN_BASELINE_VALID_DAYS
        )
        assert (
            constants["min_window_valid_fraction"]
            == DEFAULT_MIN_WINDOW_VALID_FRACTION
        )
        assert constants["rhr_baseline_days"] == DEFAULT_RHR_BASELINE_DAYS
        assert constants["rhr_band_sd"] == DEFAULT_RHR_BAND_SD
        assert (
            constants["rhr_min_baseline_valid_days"]
            == DEFAULT_RHR_MIN_BASELINE_VALID_DAYS
        )
        assert constants["sleep_baseline_days"] == DEFAULT_SLEEP_BASELINE_DAYS
        assert constants["sleep_band_sd"] == DEFAULT_SLEEP_BAND_SD
        assert (
            constants["sleep_min_baseline_valid_days"]
            == DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS
        )
        assert constants["tsb_very_negative_below"] == DEFAULT_TSB_VERY_NEGATIVE

    def test_non_default_values_flow_through_the_mapping(self) -> None:
        constants = readiness_constants_from_settings(
            _settings(
                engine_hrv_window_days=5,
                engine_hrv_baseline_days=56,
                engine_hrv_band_sd=0.75,
                engine_hrv_min_baseline_valid_days=40,
                engine_min_window_valid_fraction=0.8,
                engine_rhr_baseline_days=21,
                engine_rhr_band_sd=0.6,
                engine_rhr_min_baseline_valid_days=15,
                engine_sleep_baseline_days=14,
                engine_sleep_band_sd=1.0,
                engine_sleep_min_baseline_valid_days=10,
                engine_tsb_very_negative=-25.0,
            )
        )
        assert constants["hrv_window_days"] == 5
        assert constants["hrv_baseline_days"] == 56
        assert constants["hrv_band_sd"] == 0.75
        assert constants["hrv_min_baseline_valid_days"] == 40
        assert constants["min_window_valid_fraction"] == 0.8
        assert constants["rhr_baseline_days"] == 21
        assert constants["rhr_band_sd"] == 0.6
        assert constants["rhr_min_baseline_valid_days"] == 15
        assert constants["sleep_baseline_days"] == 14
        assert constants["sleep_band_sd"] == 1.0
        assert constants["sleep_min_baseline_valid_days"] == 10
        assert constants["tsb_very_negative_below"] == -25.0

    def test_values_flow_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_HRV_WINDOW_DAYS", "5")
        monkeypatch.setenv("ENGINE_HRV_BAND_SD", "0.75")
        monkeypatch.setenv("ENGINE_RHR_MIN_BASELINE_VALID_DAYS", "15")
        monkeypatch.setenv("ENGINE_SLEEP_BASELINE_DAYS", "14")
        monkeypatch.setenv("ENGINE_MIN_WINDOW_VALID_FRACTION", "0.8")
        monkeypatch.setenv("ENGINE_TSB_VERY_NEGATIVE", "-25")
        constants = readiness_constants_from_settings(Settings())
        assert constants["hrv_window_days"] == 5
        assert constants["hrv_band_sd"] == 0.75
        assert constants["rhr_min_baseline_valid_days"] == 15
        assert constants["sleep_baseline_days"] == 14
        assert constants["min_window_valid_fraction"] == 0.8
        assert constants["tsb_very_negative_below"] == -25.0


class TestIntensityConstantsFromSettings:
    def test_every_key_is_an_accepted_kwarg_of_its_documented_function(
        self,
    ) -> None:
        constants = intensity_constants_from_settings(_settings())
        # The threshold maps and sport→modality map are PER-MODALITY /
        # PER-SPORT dicts consumed one entry at a time by
        # ``three_zone_model``; the pin records the single-entry parameter.
        pinned = {
            "first_threshold_pcts": (three_zone_model, "first_threshold_pct"),
            "second_threshold_pcts": (
                three_zone_model,
                "second_threshold_pct",
            ),
            "min_pattern_week_seconds": (
                descriptive_pattern_comparison,
                "min_pattern_week_seconds",
            ),
            "polarized_bands": (
                descriptive_pattern_comparison,
                "polarized_bands",
            ),
            "pyramidal_bands": (
                descriptive_pattern_comparison,
                "pyramidal_bands",
            ),
            "speed_tolerance_mps": (moving_weights, "speed_tolerance_mps"),
            "gap_cap_median_multiple": (
                moving_weights,
                "gap_cap_median_multiple",
            ),
        }
        assert set(constants) == set(pinned) | {"sport_modality"}, (
            "the mapping keys and the pinned keys diverged; update both "
            "together so every constant stays wired to its consumer"
        )
        for key, (func, param) in pinned.items():
            signature = inspect.signature(cast(Any, func))
            assert param in signature.parameters, (
                f"{key!r} maps to {param!r}, which is not a keyword "
                f"parameter of {func.__name__}"
            )
        # sport_modality: every value must be a modality the 3-zone model
        # accepts (the shape itself is pinned to SPORT_MODALITY below).
        for modality in constants["sport_modality"].values():
            three_zone_model(modality)  # must not raise

    def test_defaults_match_the_engine_fallbacks(self) -> None:
        constants = intensity_constants_from_settings(_settings())
        assert constants["first_threshold_pcts"] == DEFAULT_FIRST_THRESHOLD_PCTS
        assert constants["second_threshold_pcts"] == DEFAULT_SECOND_THRESHOLD_PCTS
        assert constants["sport_modality"] == SPORT_MODALITY
        assert constants["min_pattern_week_seconds"] == DEFAULT_MIN_PATTERN_WEEK_SECONDS
        assert constants["polarized_bands"] == DEFAULT_POLARIZED_BANDS
        assert constants["pyramidal_bands"] == DEFAULT_PYRAMIDAL_BANDS
        assert (
            constants["speed_tolerance_mps"]
            == DEFAULT_PAUSE_SPEED_TOLERANCE_MPS
        )
        assert (
            constants["gap_cap_median_multiple"]
            == DEFAULT_PAUSE_GAP_CAP_MEDIAN_MULTIPLE
        )

    def test_non_default_values_flow_through_the_mapping(self) -> None:
        constants = intensity_constants_from_settings(
            _settings(
                engine_first_threshold_pcts=(
                    "bike_power:75,run_hr:89,bike_hr:89,swim_pace:94"
                ),
                engine_second_threshold_pcts=(
                    "bike_power:105,run_hr:99,bike_hr:99,swim_pace:100"
                ),
                engine_sport_modality="run:run_hr,bike:bike_hr,swim:swim_pace",
                engine_min_pattern_week_seconds=5400.0,
                engine_pause_speed_tolerance_mps=0.2,
                engine_pause_gap_cap_median_multiple=8.0,
                engine_polarized_bands="65,85,5,20,15,35",
                engine_pyramidal_bands="50,70,10,30,10,25",
            )
        )
        assert constants["first_threshold_pcts"] == {
            "bike_power": 75.0,
            "run_hr": 89.0,
            "bike_hr": 89.0,
            "swim_pace": 94.0,
        }
        assert constants["second_threshold_pcts"] == {
            "bike_power": 105.0,
            "run_hr": 99.0,
            "bike_hr": 99.0,
            "swim_pace": 100.0,
        }
        assert constants["sport_modality"] == {
            "run": "run_hr",
            "bike": "bike_hr",
            "swim": "swim_pace",
        }
        assert constants["min_pattern_week_seconds"] == 5400.0
        assert constants["speed_tolerance_mps"] == 0.2
        assert constants["gap_cap_median_multiple"] == 8.0
        assert constants["polarized_bands"] == ((65.0, 85.0), (5.0, 20.0), (15.0, 35.0))
        assert constants["pyramidal_bands"] == ((50.0, 70.0), (10.0, 30.0), (10.0, 25.0))

    def test_values_flow_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ENGINE_FIRST_THRESHOLD_PCTS",
            "bike_power:75,run_hr:89,bike_hr:89,swim_pace:94",
        )
        monkeypatch.setenv(
            "ENGINE_SPORT_MODALITY", "run:run_hr,bike:bike_hr,swim:swim_pace"
        )
        monkeypatch.setenv("ENGINE_PYRAMIDAL_BANDS", "50,70,10,30,10,25")
        monkeypatch.setenv("ENGINE_MIN_PATTERN_WEEK_SECONDS", "5400")
        constants = intensity_constants_from_settings(Settings())
        assert constants["first_threshold_pcts"]["bike_power"] == 75.0
        assert constants["sport_modality"]["bike"] == "bike_hr"
        assert constants["pyramidal_bands"] == ((50.0, 70.0), (10.0, 30.0), (10.0, 25.0))
        assert constants["min_pattern_week_seconds"] == 5400.0


class TestDurabilityConstantsFromSettings:
    def test_every_key_is_an_accepted_kwarg_of_its_documented_function(
        self,
    ) -> None:
        constants = durability_constants_from_settings(_settings())
        pinned = {
            "reference_band": (session_decoupling, "reference_band"),
            "max_intensity_drift": (session_decoupling, "max_intensity_drift"),
            "min_long_session_seconds": (
                durability_trend,
                "min_long_session_seconds",
            ),
            "window_days": (durability_trend, "window_days"),
            "min_sessions": (durability_trend, "min_sessions"),
        }
        assert set(constants) == set(pinned), (
            "the mapping keys and the pinned keys diverged; update both "
            "together so every constant stays wired to its consumer"
        )
        for key, (func, param) in pinned.items():
            signature = inspect.signature(cast(Any, func))
            assert param in signature.parameters, (
                f"{key!r} maps to {param!r}, which is not a keyword "
                f"parameter of {func.__name__}"
            )

    def test_defaults_match_the_engine_fallbacks(self) -> None:
        constants = durability_constants_from_settings(_settings())
        assert constants["reference_band"] == DEFAULT_DECOUPLING_REFERENCE_BAND
        assert constants["max_intensity_drift"] == DEFAULT_MAX_HALF_INTENSITY_DRIFT
        assert (
            constants["min_long_session_seconds"]
            == DEFAULT_MIN_LONG_SESSION_SECONDS
        )
        assert constants["window_days"] == DEFAULT_TREND_WINDOW_DAYS
        assert constants["min_sessions"] == DEFAULT_MIN_TREND_SESSIONS

    def test_non_default_values_flow_through_the_mapping(self) -> None:
        constants = durability_constants_from_settings(
            _settings(
                engine_decoupling_reference_band=0.04,
                engine_max_half_intensity_drift=0.20,
                engine_min_long_session_seconds=4500.0,
                engine_trend_window_days=56,
                engine_min_trend_sessions=4,
            )
        )
        assert constants["reference_band"] == 0.04
        assert constants["max_intensity_drift"] == 0.20
        assert constants["min_long_session_seconds"] == 4500.0
        assert constants["window_days"] == 56
        assert constants["min_sessions"] == 4

    def test_values_flow_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_DECOUPLING_REFERENCE_BAND", "0.04")
        monkeypatch.setenv("ENGINE_MAX_HALF_INTENSITY_DRIFT", "0.20")
        monkeypatch.setenv("ENGINE_MIN_LONG_SESSION_SECONDS", "4500")
        monkeypatch.setenv("ENGINE_TREND_WINDOW_DAYS", "56")
        monkeypatch.setenv("ENGINE_MIN_TREND_SESSIONS", "4")
        constants = durability_constants_from_settings(Settings())
        assert constants["reference_band"] == 0.04
        assert constants["max_intensity_drift"] == 0.20
        assert constants["min_long_session_seconds"] == 4500.0
        assert constants["window_days"] == 56
        assert constants["min_sessions"] == 4


class TestThresholdPctParserValidates:
    def test_default_shape_round_trips(self) -> None:
        assert parse_threshold_pcts(
            "bike_power:76,run_hr:90,bike_hr:90,swim_pace:95",
            field="ENGINE_FIRST_THRESHOLD_PCTS",
        ) == DEFAULT_FIRST_THRESHOLD_PCTS

    def test_tolerates_spaces_and_trailing_comma(self) -> None:
        assert parse_threshold_pcts(
            "bike_power:76, run_hr:90 , bike_hr:90,swim_pace:95,",
            field="ENGINE_FIRST_THRESHOLD_PCTS",
        ) == DEFAULT_FIRST_THRESHOLD_PCTS

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty
            "  ",  # whitespace only
            "bike_power:76,run_hr:90,bike_hr:90",  # missing swim_pace
            "bike_power:76,run_hr:90,bike_hr:90,swim_pace:95,bike_power:76",  # duplicate
            "bike_power:76,run_hr:90,bike_hr:90,swim_pace:95,rowing:80",  # unknown key
            "bike_power=76,run_hr=90,bike_hr=90,swim_pace=95",  # wrong separator
            "bike_power:abc,run_hr:90,bike_hr:90,swim_pace:95",  # non-numeric
            "bike_power:0,run_hr:90,bike_hr:90,swim_pace:95",  # non-positive
            "bike_power:-76,run_hr:90,bike_hr:90,swim_pace:95",  # negative
        ],
    )
    def test_rejects_malformed_maps(self, raw: str) -> None:
        with pytest.raises(ValueError, match="ENGINE_FIRST_THRESHOLD_PCTS"):
            parse_threshold_pcts(raw, field="ENGINE_FIRST_THRESHOLD_PCTS")


class TestSportModalityParserValidates:
    def test_default_shape_round_trips(self) -> None:
        assert parse_sport_modality(
            "run:run_hr,bike:bike_power,swim:swim_pace",
            field="ENGINE_SPORT_MODALITY",
        ) == SPORT_MODALITY

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "run:run_hr,bike:bike_power",  # missing swim
            "run:run_hr,run:run_hr,swim:swim_pace",  # duplicate sport
            "run:run_hr,bike:bike_power,swim:rowing_css",  # unknown modality
            "walk:run_hr,bike:bike_power,swim:swim_pace",  # unknown sport
            "run_run_hr,bike:bike_power,swim:swim_pace",  # missing colon
        ],
    )
    def test_rejects_malformed_maps(self, raw: str) -> None:
        with pytest.raises(ValueError, match="ENGINE_SPORT_MODALITY"):
            parse_sport_modality(raw, field="ENGINE_SPORT_MODALITY")


class TestReferenceBandParserValidates:
    def test_default_shape_round_trips(self) -> None:
        assert parse_reference_bands(
            "70,90,0,15,10,30", field="ENGINE_POLARIZED_BANDS"
        ) == DEFAULT_POLARIZED_BANDS

    def test_tolerates_spaces(self) -> None:
        assert parse_reference_bands(
            "70, 90, 0, 15, 10, 30", field="ENGINE_POLARIZED_BANDS"
        ) == DEFAULT_POLARIZED_BANDS

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty
            "70,90,0,15,10",  # five numbers
            "70,90,0,15,10,30,5",  # seven numbers
            "70,90,0,15,10,x",  # non-numeric
            "90,70,0,15,10,30",  # lo > hi in the first pair
            "70,101,0,15,10,30",  # hi above 100
            "-1,70,0,15,10,30",  # lo below 0
        ],
    )
    def test_rejects_malformed_bands(self, raw: str) -> None:
        with pytest.raises(ValueError, match="ENGINE_POLARIZED_BANDS"):
            parse_reference_bands(raw, field="ENGINE_POLARIZED_BANDS")
