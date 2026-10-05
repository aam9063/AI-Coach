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
