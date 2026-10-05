"""The read layer maps Settings onto the pure engine (LOAD-11, §7/§14).

``app.db.daily_load`` is the CLI read layer for the persistence service:
it must take the engine constants from Settings and pass them into the
pure engine — never re-hardcode them. Pure tests (no DB).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.settings import Settings
from app.db.daily_load import (
    engine_constants_from_settings,
    thresholds_from_settings,
    trimp_coefficients_from_settings,
)
from app.engine.load import ActivityLoadInput, ThresholdBundle, select_load_method


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


class TestThresholdsFromSettings:
    def test_maps_every_threshold_and_the_srpe_factor(self) -> None:
        settings = _settings(
            athlete_ftp_w=180,
            athlete_lthr_bpm=169,
            athlete_hr_max_bpm=186,
            athlete_hr_rest_bpm=65,
            athlete_css_speed_mps=0.8333,
            athlete_threshold_run_speed_mps=2.5,
            engine_srpe_tss_equivalent_factor=0.7,
        )
        bundle = thresholds_from_settings(settings)
        assert bundle == ThresholdBundle(
            ftp_watts=180.0,
            threshold_run_speed_mps=2.5,
            css_mps=0.8333,
            lthr_bpm=169.0,
            hr_max_bpm=186.0,
            hr_rest_bpm=65.0,
            srpe_tss_equivalent_factor=0.7,
        )

    def test_missing_thresholds_stay_none(self) -> None:
        bundle = thresholds_from_settings(_settings())
        assert bundle.ftp_watts is None
        assert bundle.lthr_bpm is None
        # Owner-agreed anchor default: 100/420 ≈ 0.2381 (1 h at RPE 7 ≡
        # 1 h at threshold) — a documented owner choice, not a literature
        # constant.
        assert bundle.srpe_tss_equivalent_factor == pytest.approx(100 / 420)

    def test_non_default_srpe_factor_flows_through_the_engine(self) -> None:
        bundle = thresholds_from_settings(
            _settings(engine_srpe_tss_equivalent_factor=0.7)
        )
        selection = select_load_method(
            ActivityLoadInput(sport="WeightTraining", duration_s=3600.0, rpe=7.0),
            bundle,
            coefficients=trimp_coefficients_from_settings(_settings()),
        )
        assert selection.method == "srpe"
        # 7 RPE x 60 min x 0.7 — the factor provably flows through.
        assert selection.tss == pytest.approx(294.0)


class TestTrimpCoefficientsFromSettings:
    def test_default_is_the_documented_male_banister_set(self) -> None:
        coeffs = trimp_coefficients_from_settings(_settings())
        assert (coeffs.a, coeffs.b) == (0.64, 1.92)

    def test_female_sex_selects_the_female_set(self) -> None:
        coeffs = trimp_coefficients_from_settings(_settings(engine_trimp_sex="female"))
        assert (coeffs.a, coeffs.b) == (0.86, 1.67)

    def test_overridden_coefficients_flow_through(self) -> None:
        coeffs = trimp_coefficients_from_settings(
            _settings(engine_trimp_male_a=0.5, engine_trimp_male_b=1.5)
        )
        assert (coeffs.a, coeffs.b) == (0.5, 1.5)

    def test_unknown_sex_raises_never_defaults(self) -> None:
        with pytest.raises(ValueError, match="sex"):
            trimp_coefficients_from_settings(_settings(engine_trimp_sex="horse"))


class TestEngineConstantsFromSettings:
    def test_passes_every_pmc_and_power_constant(self) -> None:
        settings = _settings(
            engine_tau_ctl_days=14,
            engine_tau_atl_days=3,
            engine_min_history_days=60,
            engine_np_window_samples=15,
            engine_min_valid_fraction=0.9,
            engine_trimp_reference_minutes=30,
        )
        assert engine_constants_from_settings(settings) == {
            "tau_ctl_days": 14.0,
            "tau_atl_days": 3.0,
            "min_history_days": 60,
            "np_window_samples": 15,
            "np_min_valid_fraction": 0.9,
            "trimp_reference_minutes": 30.0,
        }
