"""The settings→engine mapping for zone/threshold constants (ZON-11, §7/§14).

Contract (LOAD-11 pattern of ``app.db.daily_load``):
``app.db.zones_config.zone_constants_from_settings`` is a PURE mapping from
the sourced ``engine_*`` Settings fields onto the pure engine's keyword
parameters. The key names are pinned here against the signatures of the
documented engine functions, the defaults are pinned against the engine's
documented module constants (the fallbacks), and the string-ladder parsers
validate with clear ``ValueError`` s.

The helper deliberately has NO caller yet: nothing in ``app/`` consumes
zones today (Feature 6 will wire the ``get_zones`` tool and the
threshold-proposal flow); it exists so that wiring is a reviewed, tested
mapping instead of an ad-hoc read when Feature 6 lands.
"""

import inspect
from typing import Any, cast

import pytest

from app.core.settings import Settings
from app.db.zones_config import (
    parse_float_ladder,
    parse_ftp_precedence,
    parse_int_ladder,
    parse_swim_boundaries,
    zone_constants_from_settings,
)
from app.engine.zones import (
    DEFAULT_CP_TO_FTP_FACTOR,
    DEFAULT_MMP_DURATIONS_S,
    DEFAULT_SWIM_ZONE_BOUNDARY_PCTS,
    DEFAULT_THRESHOLD_CHANGE_MARGIN,
    FTP_SOURCE_KEYS,
    TWENTY_MIN_TO_FTP_FACTOR,
    fit_critical_power,
    fit_critical_speed,
    mean_maximal_power_curve,
    propose_threshold_change,
    resolve_ftp,
    swim_zones,
    training_paces,
)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


class TestZoneConstantsFromSettings:
    def test_every_key_is_an_accepted_kwarg_of_its_documented_function(
        self,
    ) -> None:
        constants = zone_constants_from_settings(_settings())
        # (key, function, engine parameter name). The CP and CS fits share
        # the parameter names min/max_duration_s, so the mapping keys are
        # namespaced by metric and the pin records the target parameter.
        pinned = {
            "cp_min_duration_s": (fit_critical_power, "min_duration_s"),
            "cp_max_duration_s": (fit_critical_power, "max_duration_s"),
            "mmp_durations_s": (mean_maximal_power_curve, "durations_s"),
            "cs_min_duration_s": (fit_critical_speed, "min_duration_s"),
            "cs_max_duration_s": (fit_critical_speed, "max_duration_s"),
            "ftp_precedence": (resolve_ftp, "precedence"),
            "cp_to_ftp_factor": (resolve_ftp, "cp_to_ftp_factor"),
            "twenty_min_to_ftp_factor": (
                resolve_ftp,
                "twenty_min_to_ftp_factor",
            ),
            "easy_pct": (training_paces, "easy_pct"),
            "marathon_pct": (training_paces, "marathon_pct"),
            "threshold_pct": (training_paces, "threshold_pct"),
            "interval_pct": (training_paces, "interval_pct"),
            "repetition_pct": (training_paces, "repetition_pct"),
            "swim_boundary_pcts_css": (swim_zones, "boundary_pcts_css"),
            "threshold_change_margin": (
                propose_threshold_change,
                "margin",
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
        constants = zone_constants_from_settings(_settings())
        assert constants["cp_min_duration_s"] == 120.0
        assert constants["cp_max_duration_s"] == 1200.0
        assert constants["mmp_durations_s"] == DEFAULT_MMP_DURATIONS_S
        assert constants["cs_min_duration_s"] == 180.0
        assert constants["cs_max_duration_s"] == 1200.0
        assert constants["ftp_precedence"] == FTP_SOURCE_KEYS
        assert constants["cp_to_ftp_factor"] == DEFAULT_CP_TO_FTP_FACTOR
        assert constants["twenty_min_to_ftp_factor"] == TWENTY_MIN_TO_FTP_FACTOR
        assert constants["easy_pct"] == 66.5
        assert constants["marathon_pct"] == 79.5
        assert constants["threshold_pct"] == 85.5
        assert constants["interval_pct"] == 97.5
        assert constants["repetition_pct"] == 112.5
        assert constants["swim_boundary_pcts_css"] == DEFAULT_SWIM_ZONE_BOUNDARY_PCTS
        assert constants["threshold_change_margin"] == DEFAULT_THRESHOLD_CHANGE_MARGIN

    def test_values_flow_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENGINE_CP_FIT_WINDOW_MIN_S", "90")
        monkeypatch.setenv("ENGINE_CP_FIT_WINDOW_MAX_S", "1500")
        monkeypatch.setenv("ENGINE_MMP_DURATIONS_S", "60, 300, 600")
        monkeypatch.setenv("ENGINE_CS_FIT_WINDOW_MIN_S", "240")
        monkeypatch.setenv("ENGINE_CP_TO_FTP_FACTOR", "0.95")
        monkeypatch.setenv("ENGINE_DANIELS_THRESHOLD_PCT_VO2MAX", "86.0")
        monkeypatch.setenv("ENGINE_THRESHOLD_CHANGE_MARGIN", "0.03")
        constants = zone_constants_from_settings(Settings())
        assert constants["cp_min_duration_s"] == 90.0
        assert constants["cp_max_duration_s"] == 1500.0
        assert constants["mmp_durations_s"] == (60, 300, 600)
        assert constants["cs_min_duration_s"] == 240.0
        assert constants["cp_to_ftp_factor"] == 0.95
        assert constants["threshold_pct"] == 86.0
        assert constants["threshold_change_margin"] == 0.03


class TestLadderParsersValidate:
    def test_int_ladder_tolerates_spaces(self) -> None:
        assert parse_int_ladder("1, 60, 300", field="ENGINE_MMP_DURATIONS_S") == (
            1,
            60,
            300,
        )

    def test_int_ladder_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="ENGINE_MMP_DURATIONS_S"):
            parse_int_ladder("  ", field="ENGINE_MMP_DURATIONS_S")

    def test_int_ladder_rejects_non_integer(self) -> None:
        with pytest.raises(ValueError, match="comma-separated integers"):
            parse_int_ladder("60,abc", field="ENGINE_MMP_DURATIONS_S")

    def test_int_ladder_rejects_non_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse_int_ladder("60,0", field="ENGINE_MMP_DURATIONS_S")

    def test_float_ladder_round_trips(self) -> None:
        assert parse_float_ladder("85, 95.5, 100", field="X") == (85.0, 95.5, 100.0)

    def test_float_ladder_rejects_non_numeric(self) -> None:
        with pytest.raises(ValueError, match="comma-separated numbers"):
            parse_float_ladder("85,x", field="X")

    def test_ftp_precedence_accepts_a_permutation(self) -> None:
        assert parse_ftp_precedence("twenty_min_power, manual ,cp_derived") == (
            "twenty_min_power",
            "manual",
            "cp_derived",
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "manual,manual,cp_derived",  # duplicate
            "manual,cp_derived",  # missing key
            "manual,cp_derived,strava",  # unknown key
            "",
        ],
    )
    def test_ftp_precedence_rejects_non_permutations(self, raw: str) -> None:
        with pytest.raises(ValueError, match="ENGINE_FTP_PRECEDENCE"):
            parse_ftp_precedence(raw)

    def test_swim_boundaries_accepts_the_default_shape(self) -> None:
        assert parse_swim_boundaries("85,95,100,105") == (85.0, 95.0, 100.0, 105.0)

    @pytest.mark.parametrize(
        "raw",
        ["85,95,100", "85,95,100,105,110", "85,95,95,105", "105,95,100,90", "0,95,100,105"],
    )
    def test_swim_boundaries_rejects_invalid_tuples(self, raw: str) -> None:
        with pytest.raises(ValueError, match="ENGINE_SWIM_ZONE_BOUNDARY_PCTS"):
            parse_swim_boundaries(raw)
