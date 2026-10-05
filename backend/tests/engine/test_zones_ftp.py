"""FTP resolution with a stated source (ZON-3, PROJECT_BRIEF section 7.3).

The brief requires FTP to be configurable from three sources — manual value,
CP-derived estimate, or 95% of best 20-minute power — and the output to
ALWAYS state which one is in use. Every expected value here is hand-derived:

- Manual: the owner's FTP of record is a manual 180 W. The owner has NO
  power meter, so the manual source is the path that will be used in
  practice and is tested as such.
- CP-derived: the ZON-2 fit on the exact linear-model curve
  (CP = 250 W, W' = 18000 J) recovers CP = 250.0 W to machine precision
  (hand arithmetic: P(180 s) = 250 + 18000/180 = 350 W, P(300 s) = 310 W,
  P(1200 s) = 265 W), so with the default factor 1.0 the derived FTP is
  exactly 250.0 W, and with an explicit factor 0.95 exactly 237.5 W.
- 20-minute power: 95% of a best 20-min power of 265.0 W is
  0.95 * 265.0 = 251.75 W (Allen & Coggan FTP convention).

Precedence (documented OWNER CHOICE): manual > CP-derived > 20-min power —
the owner's manually confirmed value is the FTP of record (human-in-the-loop,
section 7.3) and always wins while configured.
"""

import dataclasses
import inspect
from typing import Final

import pytest

import app.engine.zones as _zones_module
from app.engine.zones import (
    CriticalPowerFit,
    fit_critical_power,
    resolve_ftp,
)

CP: Final = 250.0
W_PRIME: Final = 18000.0


def exact_cp_fit() -> CriticalPowerFit:
    """Fit of the exact linear-model curve (CP 250 W, W' 18000 J): CP = 250.0 W."""
    curve = {
        180: CP + W_PRIME / 180,  # 350.0 W
        300: CP + W_PRIME / 300,  # 310.0 W
        1200: CP + W_PRIME / 1200,  # 265.0 W
    }
    return fit_critical_power(curve)


class TestManualSource:
    def test_manual_value_is_reported_with_its_source(self) -> None:
        result = resolve_ftp(manual_ftp_watts=180.0)
        assert result.ftp_watts == 180.0
        assert result.source == "manual"
        assert "manual" in result.detail.lower()

    def test_result_is_frozen(self) -> None:
        result = resolve_ftp(manual_ftp_watts=180.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.ftp_watts = 200.0  # type: ignore[misc]

    def test_module_documents_the_owner_manual_180w_path(self) -> None:
        # The owner has no power meter: the manual 180 W value is the FTP of
        # record and the path used in practice — the module must say so.
        doc = (inspect.getdoc(_zones_module) or "").lower()
        assert "no power meter" in doc
        assert "180" in doc

    def test_non_positive_manual_value_raises(self) -> None:
        with pytest.raises(ValueError, match="manual"):
            resolve_ftp(manual_ftp_watts=0.0)
        with pytest.raises(ValueError, match="manual"):
            resolve_ftp(manual_ftp_watts=-5.0)


class TestCpDerivedSource:
    def test_cp_derived_with_default_factor_is_the_cp_value(self) -> None:
        result = resolve_ftp(cp_fit=exact_cp_fit())
        assert result.ftp_watts == pytest.approx(250.0)  # CP 250.0 W x 1.0
        assert result.source == "cp_derived"
        assert result.cp_watts == pytest.approx(250.0)
        assert result.cp_to_ftp_factor == 1.0

    def test_cp_factor_is_configurable(self) -> None:
        result = resolve_ftp(cp_fit=exact_cp_fit(), cp_to_ftp_factor=0.95)
        assert result.ftp_watts == pytest.approx(237.5)  # 250.0 x 0.95
        assert result.cp_to_ftp_factor == 0.95

    def test_factor_is_explicit_in_the_detail_not_buried(self) -> None:
        result = resolve_ftp(cp_fit=exact_cp_fit())
        assert "1.0" in result.detail
        doc = inspect.getdoc(resolve_ftp) or ""
        assert "owner" in doc.lower()
        assert "convention" in doc.lower()

    def test_non_positive_cp_raises(self) -> None:
        bad_fit = CriticalPowerFit(
            cp_watts=0.0, w_prime_joules=18000.0, r_squared=1.0, rmse_joules=0.0,
            n_points=3,
        )
        with pytest.raises(ValueError, match="CP"):
            resolve_ftp(cp_fit=bad_fit)

    def test_non_positive_factor_raises(self) -> None:
        with pytest.raises(ValueError, match="factor"):
            resolve_ftp(cp_fit=exact_cp_fit(), cp_to_ftp_factor=0.0)


class TestTwentyMinPowerSource:
    def test_ninety_five_percent_of_the_600s_curve_point(self) -> None:
        result = resolve_ftp(curve={600: 265.0})
        assert result.ftp_watts == pytest.approx(251.75)  # 0.95 * 265.0
        assert result.source == "twenty_min_power"
        assert result.best_20_min_power_watts == pytest.approx(265.0)
        assert "95" in result.detail

    def test_direct_twenty_min_power_overrides_the_curve(self) -> None:
        result = resolve_ftp(curve={600: 300.0}, twenty_min_power_watts=265.0)
        assert result.ftp_watts == pytest.approx(251.75)

    def test_curve_without_600s_point_leaves_the_source_unavailable(self) -> None:
        with pytest.raises(ValueError):
            resolve_ftp(curve={300: 310.0})

    def test_non_positive_twenty_min_power_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_ftp(twenty_min_power_watts=0.0)
        with pytest.raises(ValueError):
            resolve_ftp(curve={600: -265.0})

    def test_twenty_min_factor_is_configurable(self) -> None:
        # ZON-11: the 0.95 Allen & Coggan constant is an explicit parameter
        # (settings-mirrored as ENGINE_TWENTY_MIN_TO_FTP_FACTOR); the
        # module constant stays the documented fallback.
        result = resolve_ftp(
            twenty_min_power_watts=265.0, twenty_min_to_ftp_factor=0.90
        )
        assert result.source == "twenty_min_power"
        assert result.ftp_watts == pytest.approx(238.5)  # 0.90 * 265.0
        assert "0.9" in result.detail

    def test_non_positive_twenty_min_factor_raises(self) -> None:
        with pytest.raises(ValueError, match="factor"):
            resolve_ftp(
                twenty_min_power_watts=265.0, twenty_min_to_ftp_factor=0.0
            )


class TestPrecedence:
    def test_all_three_available_manual_wins_by_default(self) -> None:
        result = resolve_ftp(
            manual_ftp_watts=180.0, cp_fit=exact_cp_fit(), curve={600: 265.0}
        )
        assert result.source == "manual"
        assert result.ftp_watts == 180.0

    def test_without_manual_the_cp_derived_estimate_wins(self) -> None:
        result = resolve_ftp(cp_fit=exact_cp_fit(), curve={600: 265.0})
        assert result.source == "cp_derived"
        assert result.ftp_watts == pytest.approx(250.0)

    def test_without_manual_and_cp_the_twenty_min_estimate_wins(self) -> None:
        result = resolve_ftp(curve={600: 265.0})
        assert result.source == "twenty_min_power"
        assert result.ftp_watts == pytest.approx(251.75)

    def test_custom_precedence_overrides_the_default(self) -> None:
        result = resolve_ftp(
            manual_ftp_watts=180.0,
            cp_fit=exact_cp_fit(),
            curve={600: 265.0},
            precedence=("twenty_min_power", "cp_derived", "manual"),
        )
        assert result.source == "twenty_min_power"
        assert result.ftp_watts == pytest.approx(251.75)

    def test_available_sources_follow_the_precedence_order(self) -> None:
        result = resolve_ftp(manual_ftp_watts=180.0, curve={600: 265.0})
        assert result.available_sources == ("manual", "twenty_min_power")

    def test_precedence_must_be_a_permutation_of_the_three_keys(self) -> None:
        with pytest.raises(ValueError, match="precedence"):
            resolve_ftp(
                manual_ftp_watts=180.0,
                precedence=("manual", "cp_derived"),
            )
        with pytest.raises(ValueError, match="precedence"):
            resolve_ftp(
                manual_ftp_watts=180.0,
                precedence=("manual", "cp_derived", "cp_derived"),
            )


class TestNoSourceAvailable:
    def test_error_names_all_three_sources(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            resolve_ftp()
        message = str(excinfo.value)
        assert "manual" in message
        assert "cp_derived" in message
        assert "twenty_min_power" in message
