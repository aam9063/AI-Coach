"""Settings carry every engine constant, sourced, with engine-matching
defaults (LOAD-11, §7/§14).

Contract: the Settings defaults are EXACTLY today's effective engine
values (the pure engine's documented module constants remain the fallback),
each configurable via environment variables, and the sRPE TSS-equivalent
factor is the documented OWNER CHOICE (equivalent-effort anchor,
2026-10-05: 100/420).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.settings import Settings
from app.db.engine_readiness_config import (
    parse_reference_bands,
    parse_sport_modality,
    parse_threshold_pcts,
)
from app.db.zones_config import (
    parse_ftp_precedence,
    parse_int_ladder,
    parse_swim_boundaries,
)
from app.engine.banister import (
    DEFAULT_MIN_MARKERS,
    DEFAULT_TAU1_DAYS,
    DEFAULT_TAU2_DAYS,
)
from app.engine.durability import (
    DEFAULT_DECOUPLING_REFERENCE_BAND,
    DEFAULT_MAX_HALF_INTENSITY_DRIFT,
    DEFAULT_MIN_LONG_SESSION_SECONDS,
    DEFAULT_MIN_TREND_SESSIONS,
    DEFAULT_TREND_WINDOW_DAYS,
)
from app.engine.intensity import (
    DEFAULT_FIRST_THRESHOLD_PCTS,
    DEFAULT_MIN_PATTERN_WEEK_SECONDS,
    DEFAULT_POLARIZED_BANDS,
    DEFAULT_PYRAMIDAL_BANDS,
    DEFAULT_SECOND_THRESHOLD_PCTS,
    SPORT_MODALITY,
)
from app.engine.load import NP_WINDOW_SAMPLES
from app.engine.pmc import (
    ACWR_TAU_ACUTE_DAYS,
    ACWR_TAU_CHRONIC_DAYS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_TAU_ATL_DAYS,
    DEFAULT_TAU_CTL_DAYS,
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
)
from app.engine.zones import (
    CP_MAX_DURATION_S,
    CP_MIN_DURATION_S,
    CS_MAX_DURATION_S,
    CS_MIN_DURATION_S,
    DEFAULT_CP_TO_FTP_FACTOR,
    DEFAULT_EASY_PCT_VO2MAX,
    DEFAULT_FTP_PRECEDENCE,
    DEFAULT_INTERVAL_PCT_VO2MAX,
    DEFAULT_MARATHON_PCT_VO2MAX,
    DEFAULT_MMP_DURATIONS_S,
    DEFAULT_REPETITION_PCT_VO2MAX,
    DEFAULT_SWIM_ZONE_BOUNDARY_PCTS,
    DEFAULT_THRESHOLD_CHANGE_MARGIN,
    DEFAULT_THRESHOLD_PCT_VO2MAX,
    TWENTY_MIN_TO_FTP_FACTOR,
)
from app.tools.cross_check_pmc import (
    DEFAULT_ABSOLUTE_TOLERANCE,
    DEFAULT_DISCONTINUITY_THRESHOLD,
    DEFAULT_TOLERANCE,
)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


class TestSettingsDefaultsMatchEngineConstants:
    def test_pmc_constants(self) -> None:
        settings = _settings()
        assert settings.engine_tau_ctl_days == DEFAULT_TAU_CTL_DAYS == 42.0
        assert settings.engine_tau_atl_days == DEFAULT_TAU_ATL_DAYS == 7.0
        assert settings.engine_min_history_days == DEFAULT_MIN_HISTORY_DAYS == 90

    def test_acwr_constants(self) -> None:
        settings = _settings()
        assert settings.engine_acwr_tau_acute_days == ACWR_TAU_ACUTE_DAYS == 7.0
        assert settings.engine_acwr_tau_chronic_days == ACWR_TAU_CHRONIC_DAYS == 28.0

    def test_power_constants(self) -> None:
        settings = _settings()
        assert settings.engine_np_window_samples == NP_WINDOW_SAMPLES == 30
        assert settings.engine_min_valid_fraction == 1.0

    def test_trimp_constants(self) -> None:
        settings = _settings()
        assert settings.engine_trimp_male_a == 0.64
        assert settings.engine_trimp_male_b == 1.92
        assert settings.engine_trimp_female_a == 0.86
        assert settings.engine_trimp_female_b == 1.67
        assert settings.engine_trimp_sex == "male"
        assert settings.engine_trimp_reference_minutes == 60.0

    def test_srpe_factor_default_is_the_owner_anchor(self) -> None:
        # 100 TSS per 420 Foster AU (1 h at RPE 7) = 100 / 420 =
        # 0.2380952380952381 — the owner-agreed equivalent-effort anchor.
        assert _settings().engine_srpe_tss_equivalent_factor == pytest.approx(
            100 / 420
        )

    def test_banister_constants(self) -> None:
        settings = _settings()
        assert settings.engine_banister_tau1_days == DEFAULT_TAU1_DAYS == 42.0
        assert settings.engine_banister_tau2_days == DEFAULT_TAU2_DAYS == 7.0
        assert settings.engine_banister_min_markers == DEFAULT_MIN_MARKERS == 10

    def test_cross_check_constants(self) -> None:
        settings = _settings()
        assert settings.engine_cross_check_relative_tolerance == DEFAULT_TOLERANCE
        assert (
            settings.engine_cross_check_absolute_tolerance
            == DEFAULT_ABSOLUTE_TOLERANCE
        )
        assert (
            settings.engine_cross_check_revision_threshold
            == DEFAULT_DISCONTINUITY_THRESHOLD
        )


class TestSettingsAreConfigurableFromEnvironment:
    def test_engine_constants_parse_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_TAU_CTL_DAYS", "14")
        monkeypatch.setenv("ENGINE_MIN_HISTORY_DAYS", "60")
        monkeypatch.setenv("ENGINE_MIN_VALID_FRACTION", "0.9")
        monkeypatch.setenv("ENGINE_TRIMP_SEX", "female")
        monkeypatch.setenv("ENGINE_CROSS_CHECK_RELATIVE_TOLERANCE", "0.2")
        settings = _settings()
        assert settings.engine_tau_ctl_days == 14.0
        assert settings.engine_min_history_days == 60
        assert settings.engine_min_valid_fraction == 0.9
        assert settings.engine_trimp_sex == "female"
        assert settings.engine_cross_check_relative_tolerance == 0.2


class TestSrpeOwnerChoiceDocumented:
    def test_field_description_says_owner_choice_and_anchor(self) -> None:
        description = (
            Settings.model_fields["engine_srpe_tss_equivalent_factor"].description
            or ""
        ).lower()
        assert "owner" in description
        assert "anchor" in description
        assert "420" in description
        assert "hrtss" in description  # the rejected alternative is named


class TestZoneSettingsDefaultsMatchEngineConstants:
    """Every zone/threshold constant is a sourced, configurable field (ZON-11).

    Contract: the Settings defaults are EXACTLY the pure engine's documented
    module constants in ``app.engine.zones`` (the fallbacks); the mapping
    helper ``app.db.zones_config.zone_constants_from_settings`` passes them
    into the engine, which never imports settings (§6).
    """

    def test_cp_fit_window(self) -> None:
        # LITERATURE: 2-20 min linear CP window (Jones et al. 2019; §7.3).
        assert _settings().engine_cp_fit_window_min_s == CP_MIN_DURATION_S == 120.0
        assert _settings().engine_cp_fit_window_max_s == CP_MAX_DURATION_S == 1200.0

    def test_mmp_duration_ladder(self) -> None:
        # OWNER CHOICE: chart-friendly ladder; parsed from a comma-separated
        # env string into the engine's tuple.
        mmp_ladder = parse_int_ladder(
            _settings().engine_mmp_durations_s, field="ENGINE_MMP_DURATIONS_S"
        )
        assert mmp_ladder == DEFAULT_MMP_DURATIONS_S

    def test_cs_fit_window(self) -> None:
        # LITERATURE: best run efforts "roughly 3 and 20 minutes" (§7.3).
        assert _settings().engine_cs_fit_window_min_s == CS_MIN_DURATION_S == 180.0
        assert _settings().engine_cs_fit_window_max_s == CS_MAX_DURATION_S == 1200.0

    def test_ftp_precedence_and_factors(self) -> None:
        # Precedence: OWNER CHOICE (manual FTP is the value of record).
        # CP-to-FTP factor 1.0: standard convention + OWNER CHOICE.
        # 0.95 of best 20-min power: LITERATURE (Allen & Coggan).
        assert parse_ftp_precedence(_settings().engine_ftp_precedence) == DEFAULT_FTP_PRECEDENCE
        assert _settings().engine_cp_to_ftp_factor == DEFAULT_CP_TO_FTP_FACTOR == 1.0
        assert (
            _settings().engine_twenty_min_to_ftp_factor
            == TWENTY_MIN_TO_FTP_FACTOR
            == 0.95
        )

    def test_daniels_band_midpoints(self) -> None:
        # Bands LITERATURE (Daniels' Running Formula, 3rd ed., 2013);
        # midpoints OWNER-REVIEWABLE choices inside the bands.
        assert _settings().engine_daniels_easy_pct_vo2max == DEFAULT_EASY_PCT_VO2MAX == 66.5
        assert _settings().engine_daniels_marathon_pct_vo2max == DEFAULT_MARATHON_PCT_VO2MAX == 79.5
        assert (
            _settings().engine_daniels_threshold_pct_vo2max
            == DEFAULT_THRESHOLD_PCT_VO2MAX
            == 85.5
        )
        assert _settings().engine_daniels_interval_pct_vo2max == DEFAULT_INTERVAL_PCT_VO2MAX == 97.5
        assert (
            _settings().engine_daniels_repetition_pct_vo2max
            == DEFAULT_REPETITION_PCT_VO2MAX
            == 112.5
        )

    def test_swim_zone_boundaries(self) -> None:
        # OWNER-REVIEWABLE boundaries (§7.3 publishes no swim table); the
        # 100% anchor is LITERATURE (Wakayoshi et al. 1992).
        assert (
            parse_swim_boundaries(_settings().engine_swim_zone_boundary_pcts)
            == DEFAULT_SWIM_ZONE_BOUNDARY_PCTS
            == (85.0, 95.0, 100.0, 105.0)
        )

    def test_threshold_change_margin(self) -> None:
        # ZON-9 margin: OWNER CHOICE (pending owner confirmation).
        assert _settings().engine_threshold_change_margin == DEFAULT_THRESHOLD_CHANGE_MARGIN == 0.05


class TestReadinessSettingsDefaultsMatchEngineConstants:
    """Every readiness constant is a sourced, configurable field (RID-10).

    Contract: the Settings defaults are EXACTLY the pure engine's documented
    module constants in ``app.engine.readiness`` (the fallbacks); the
    mapping helper ``app.db.engine_readiness_config`` passes them into the
    engine, which never imports settings (§6).
    """

    def test_hrv_constants(self) -> None:
        # LITERATURE: 7-day window / 60-day baseline (Plews et al. 2013),
        # ± 0.5 SD smallest-worthwhile-change band (Kiviniemi et al. 2007).
        # OWNER CHOICE: 70% baseline completeness (42 of 60 days).
        settings = _settings()
        assert settings.engine_hrv_window_days == DEFAULT_HRV_WINDOW_DAYS == 7
        assert settings.engine_hrv_baseline_days == DEFAULT_HRV_BASELINE_DAYS == 60
        assert settings.engine_hrv_band_sd == DEFAULT_HRV_BAND_SD == 0.5
        assert (
            settings.engine_hrv_min_baseline_valid_days
            == DEFAULT_HRV_MIN_BASELINE_VALID_DAYS
            == 42
        )

    def test_resting_hr_constants(self) -> None:
        # Baseline window LITERATURE (§7.4); band and completeness floor
        # OWNER CHOICE (SWC logic extended to resting HR).
        settings = _settings()
        assert settings.engine_rhr_baseline_days == DEFAULT_RHR_BASELINE_DAYS == 30
        assert settings.engine_rhr_band_sd == DEFAULT_RHR_BAND_SD == 0.5
        assert (
            settings.engine_rhr_min_baseline_valid_days
            == DEFAULT_RHR_MIN_BASELINE_VALID_DAYS
            == 21
        )

    def test_sleep_constants(self) -> None:
        # All OWNER CHOICE (the brief fixes no sleep window); symmetric with
        # resting HR.
        settings = _settings()
        assert settings.engine_sleep_baseline_days == DEFAULT_SLEEP_BASELINE_DAYS == 30
        assert settings.engine_sleep_band_sd == DEFAULT_SLEEP_BAND_SD == 0.5
        assert (
            settings.engine_sleep_min_baseline_valid_days
            == DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS
            == 21
        )

    def test_window_completeness_and_tsb_threshold(self) -> None:
        # OWNER CHOICE: strict 1.0 window completeness; TSB "very negative"
        # threshold -10.0 (the brief gives no number).
        settings = _settings()
        assert (
            settings.engine_min_window_valid_fraction
            == DEFAULT_MIN_WINDOW_VALID_FRACTION
            == 1.0
        )
        assert settings.engine_tsb_very_negative == DEFAULT_TSB_VERY_NEGATIVE == -10.0


class TestIntensitySettingsDefaultsMatchEngineConstants:
    """Every intensity constant is a sourced, configurable field (RID-10)."""

    def test_threshold_pct_maps(self) -> None:
        # First thresholds (top of source Z2): OWNER CHOICE. Second
        # thresholds (top of the published Threshold zone): LITERATURE cut
        # points (Coggan Z4, Friel LTHR, CSS = Wakayoshi et al. 1992).
        settings = _settings()
        assert parse_threshold_pcts(
            settings.engine_first_threshold_pcts,
            field="ENGINE_FIRST_THRESHOLD_PCTS",
        ) == DEFAULT_FIRST_THRESHOLD_PCTS == {
            "bike_power": 76.0,
            "run_hr": 90.0,
            "bike_hr": 90.0,
            "swim_pace": 95.0,
        }
        assert parse_threshold_pcts(
            settings.engine_second_threshold_pcts,
            field="ENGINE_SECOND_THRESHOLD_PCTS",
        ) == DEFAULT_SECOND_THRESHOLD_PCTS == {
            "bike_power": 106.0,
            "run_hr": 100.0,
            "bike_hr": 100.0,
            "swim_pace": 100.0,
        }

    def test_sport_modality_map(self) -> None:
        # OWNER CHOICE: bike -> Coggan power, run -> Friel run HR,
        # swim -> CSS pace.
        assert parse_sport_modality(
            _settings().engine_sport_modality, field="ENGINE_SPORT_MODALITY"
        ) == SPORT_MODALITY == {"run": "run_hr", "bike": "bike_power", "swim": "swim_pace"}

    def test_pattern_bands_and_minimum_volume(self) -> None:
        # Bands OWNER-REVIEWABLE around the published point values (Seiler
        # 2010 polarized; Stöggl & Sperlich 2014 pyramidal). Minimum volume
        # 2 h: OWNER CHOICE (the brief fixes no number).
        settings = _settings()
        assert parse_reference_bands(
            settings.engine_polarized_bands, field="ENGINE_POLARIZED_BANDS"
        ) == DEFAULT_POLARIZED_BANDS == ((70.0, 90.0), (0.0, 15.0), (10.0, 30.0))
        assert parse_reference_bands(
            settings.engine_pyramidal_bands, field="ENGINE_PYRAMIDAL_BANDS"
        ) == DEFAULT_PYRAMIDAL_BANDS == ((55.0, 75.0), (15.0, 35.0), (5.0, 20.0))
        assert (
            settings.engine_min_pattern_week_seconds
            == DEFAULT_MIN_PATTERN_WEEK_SECONDS
            == 7200.0
        )


class TestDurabilitySettingsDefaultsMatchEngineConstants:
    """Every durability constant is a sourced, configurable field (RID-10)."""

    def test_decoupling_constants(self) -> None:
        # Reference band LITERATURE (Friel < 5%); steadiness drift limit
        # OWNER CHOICE (no published threshold).
        settings = _settings()
        assert (
            settings.engine_decoupling_reference_band
            == DEFAULT_DECOUPLING_REFERENCE_BAND
            == 0.05
        )
        assert (
            settings.engine_max_half_intensity_drift
            == DEFAULT_MAX_HALF_INTENSITY_DRIFT
            == 0.15
        )

    def test_trend_constants(self) -> None:
        # All OWNER CHOICE: Maunder et al. 2021 motivate the trend itself,
        # not the operational thresholds.
        settings = _settings()
        assert (
            settings.engine_min_long_session_seconds
            == DEFAULT_MIN_LONG_SESSION_SECONDS
            == 5400.0
        )
        assert settings.engine_trend_window_days == DEFAULT_TREND_WINDOW_DAYS == 84
        assert settings.engine_min_trend_sessions == DEFAULT_MIN_TREND_SESSIONS == 3


class TestRid10SettingsAreConfigurableFromEnvironment:
    def test_readiness_constants_parse_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_HRV_WINDOW_DAYS", "5")
        monkeypatch.setenv("ENGINE_HRV_BASELINE_DAYS", "56")
        monkeypatch.setenv("ENGINE_HRV_BAND_SD", "0.75")
        monkeypatch.setenv("ENGINE_HRV_MIN_BASELINE_VALID_DAYS", "40")
        monkeypatch.setenv("ENGINE_RHR_BASELINE_DAYS", "21")
        monkeypatch.setenv("ENGINE_SLEEP_BAND_SD", "0.6")
        monkeypatch.setenv("ENGINE_MIN_WINDOW_VALID_FRACTION", "0.8")
        monkeypatch.setenv("ENGINE_TSB_VERY_NEGATIVE", "-15")
        settings = _settings()
        assert settings.engine_hrv_window_days == 5
        assert settings.engine_hrv_baseline_days == 56
        assert settings.engine_hrv_band_sd == 0.75
        assert settings.engine_hrv_min_baseline_valid_days == 40
        assert settings.engine_rhr_baseline_days == 21
        assert settings.engine_sleep_band_sd == 0.6
        assert settings.engine_min_window_valid_fraction == 0.8
        assert settings.engine_tsb_very_negative == -15.0

    def test_intensity_constants_parse_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ENGINE_FIRST_THRESHOLD_PCTS",
            "bike_power:75,run_hr:89,bike_hr:89,swim_pace:94",
        )
        monkeypatch.setenv(
            "ENGINE_SECOND_THRESHOLD_PCTS",
            "bike_power:105,run_hr:99,bike_hr:99,swim_pace:100",
        )
        monkeypatch.setenv(
            "ENGINE_SPORT_MODALITY", "run:run_hr,bike:bike_hr,swim:swim_pace"
        )
        monkeypatch.setenv("ENGINE_POLARIZED_BANDS", "65,85,5,20,15,35")
        monkeypatch.setenv("ENGINE_MIN_PATTERN_WEEK_SECONDS", "5400")
        settings = _settings()
        assert parse_threshold_pcts(
            settings.engine_first_threshold_pcts,
            field="ENGINE_FIRST_THRESHOLD_PCTS",
        ) == {
            "bike_power": 75.0,
            "run_hr": 89.0,
            "bike_hr": 89.0,
            "swim_pace": 94.0,
        }
        assert parse_sport_modality(
            settings.engine_sport_modality, field="ENGINE_SPORT_MODALITY"
        ) == {"run": "run_hr", "bike": "bike_hr", "swim": "swim_pace"}
        assert parse_reference_bands(
            settings.engine_polarized_bands, field="ENGINE_POLARIZED_BANDS"
        ) == ((65.0, 85.0), (5.0, 20.0), (15.0, 35.0))
        assert settings.engine_min_pattern_week_seconds == 5400.0

    def test_durability_constants_parse_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_DECOUPLING_REFERENCE_BAND", "0.04")
        monkeypatch.setenv("ENGINE_MAX_HALF_INTENSITY_DRIFT", "0.20")
        monkeypatch.setenv("ENGINE_MIN_LONG_SESSION_SECONDS", "4500")
        monkeypatch.setenv("ENGINE_TREND_WINDOW_DAYS", "56")
        monkeypatch.setenv("ENGINE_MIN_TREND_SESSIONS", "4")
        settings = _settings()
        assert settings.engine_decoupling_reference_band == 0.04
        assert settings.engine_max_half_intensity_drift == 0.20
        assert settings.engine_min_long_session_seconds == 4500.0
        assert settings.engine_trend_window_days == 56
        assert settings.engine_min_trend_sessions == 4


class TestZoneSettingsAreConfigurableFromEnvironment:
    def test_zone_constants_parse_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_CP_FIT_WINDOW_MIN_S", "90")
        monkeypatch.setenv("ENGINE_MMP_DURATIONS_S", "60,300,600")
        monkeypatch.setenv("ENGINE_FTP_PRECEDENCE", "twenty_min_power,manual,cp_derived")
        monkeypatch.setenv("ENGINE_TWENTY_MIN_TO_FTP_FACTOR", "0.90")
        monkeypatch.setenv("ENGINE_DANIELS_EASY_PCT_VO2MAX", "65.0")
        monkeypatch.setenv("ENGINE_SWIM_ZONE_BOUNDARY_PCTS", "80,90,100,110")
        monkeypatch.setenv("ENGINE_THRESHOLD_CHANGE_MARGIN", "0.08")
        settings = _settings()
        assert settings.engine_cp_fit_window_min_s == 90.0
        assert parse_int_ladder(
            settings.engine_mmp_durations_s, field="ENGINE_MMP_DURATIONS_S"
        ) == (60, 300, 600)
        assert parse_ftp_precedence(settings.engine_ftp_precedence) == (
            "twenty_min_power",
            "manual",
            "cp_derived",
        )
        assert settings.engine_twenty_min_to_ftp_factor == 0.90
        assert settings.engine_daniels_easy_pct_vo2max == 65.0
        assert parse_swim_boundaries(settings.engine_swim_zone_boundary_pcts) == (
            80.0,
            90.0,
            100.0,
            110.0,
        )
        assert settings.engine_threshold_change_margin == 0.08
