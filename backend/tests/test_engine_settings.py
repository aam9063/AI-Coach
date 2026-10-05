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
from app.engine.load import NP_WINDOW_SAMPLES
from app.engine.pmc import (
    ACWR_TAU_ACUTE_DAYS,
    ACWR_TAU_CHRONIC_DAYS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_TAU_ATL_DAYS,
    DEFAULT_TAU_CTL_DAYS,
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
