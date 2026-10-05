"""Engine constants are explicit, sourced and configurable (LOAD-11, §7/§14).

The engine stays pure: every constant is a *parameter* whose default is the
documented module constant (the documented fallback), and the Settings/CLI/
service layer reads the values and passes them in. These tests pin:

- the engine module defaults still match the documented values,
- the newly parameterized entry points accept and use non-default values,
- the sRPE TSS-equivalent factor is documented as an explicit OWNER CHOICE
  (equivalent-effort anchor: 1 h at RPE 7 = 420 AU ≡ 1 h at threshold =
  100 TSS, so 100/420).
"""

import inspect
import typing

import pytest

from app.engine.banister import (
    DEFAULT_MIN_MARKERS,
    DEFAULT_TAU1_DAYS,
    DEFAULT_TAU2_DAYS,
    evaluate_banister,
    fit_banister,
)
from app.engine.load import (
    NP_WINDOW_SAMPLES,
    ActivityLoadInput,
    BikePowerLoad,
    ThresholdBundle,
    TrimpCoefficients,
    bike_power_load,
    minetti_energy_cost,
    normalized_power,
    power_tss,
    select_load_method,
    srpe_load,
    trimp_at_lthr_reference,
)
from app.engine.pmc import (
    ACWR_TAU_ACUTE_DAYS,
    ACWR_TAU_CHRONIC_DAYS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_TAU_ATL_DAYS,
    DEFAULT_TAU_CTL_DAYS,
    acwr_ewma,
    compute_pmc,
)
from app.tools.cross_check_pmc import (
    DEFAULT_ABSOLUTE_TOLERANCE,
    DEFAULT_DISCONTINUITY_THRESHOLD,
    DEFAULT_TOLERANCE,
)


class TestEngineDefaultsMatchDocumentedValues:
    """Module constants and signature defaults stay the documented fallback."""

    def test_module_constants(self) -> None:
        assert DEFAULT_TAU_CTL_DAYS == 42.0  # Allen & Coggan, PMC chronic
        assert DEFAULT_TAU_ATL_DAYS == 7.0  # Allen & Coggan, PMC acute
        assert DEFAULT_MIN_HISTORY_DAYS == 90  # owner choice (~2 tau_ctl)
        assert ACWR_TAU_ACUTE_DAYS == 7.0  # Williams et al. 2017
        assert ACWR_TAU_CHRONIC_DAYS == 28.0  # Williams et al. 2017
        assert NP_WINDOW_SAMPLES == 30  # Coggan NP definition (1 Hz stream)
        assert DEFAULT_TAU1_DAYS == 42.0  # Banister 1991 / Morton et al. 1990
        assert DEFAULT_TAU2_DAYS == 7.0  # Banister 1991 / Morton et al. 1990
        assert DEFAULT_MIN_MARKERS == 10  # owner choice (2x 5 free parameters)
        assert DEFAULT_TOLERANCE == 0.10  # OWNER-AGREED (§12.3)
        assert DEFAULT_ABSOLUTE_TOLERANCE == 0.5  # OWNER-AGREED (§12.3)
        assert 0.0 < DEFAULT_DISCONTINUITY_THRESHOLD == 0.25  # owner choice

    def test_signature_defaults(self) -> None:
        assert (
            inspect.signature(normalized_power).parameters["min_valid_fraction"].default
            == 1.0
        )
        assert (
            inspect.signature(normalized_power).parameters["window_samples"].default
            == NP_WINDOW_SAMPLES
        )
        assert (
            inspect.signature(trimp_at_lthr_reference).parameters["duration_min"].default
            == 60.0
        )
        assert (
            inspect.signature(compute_pmc).parameters["tau_ctl_days"].default
            == DEFAULT_TAU_CTL_DAYS
        )
        assert (
            inspect.signature(compute_pmc).parameters["min_history_days"].default
            == DEFAULT_MIN_HISTORY_DAYS
        )
        assert (
            inspect.signature(acwr_ewma).parameters["tau_acute_days"].default
            == ACWR_TAU_ACUTE_DAYS
        )
        assert (
            inspect.signature(fit_banister).parameters["min_markers"].default
            == DEFAULT_MIN_MARKERS
        )
        assert (
            inspect.signature(evaluate_banister).parameters["tau1_days"].default
            == DEFAULT_TAU1_DAYS
        )
        assert (
            inspect.signature(select_load_method).parameters[
                "trimp_reference_minutes"
            ].default
            == 60.0
        )
        assert (
            inspect.signature(select_load_method).parameters[
                "np_window_samples"
            ].default
            == NP_WINDOW_SAMPLES
        )
        assert (
            inspect.signature(select_load_method).parameters[
                "np_min_valid_fraction"
            ].default
            == 1.0
        )


class TestNormalizedPowerWindowParameter:
    """NP's rolling window length is a parameter (default 30, Coggan)."""

    STREAM: typing.ClassVar[list[float]] = [100.0, 200.0, 300.0, 200.0]

    def test_window_2_hand_computed(self) -> None:
        # Windows of 2: means 150, 250, 250 -> NP = mean(150^4, 250^4, 250^4)^(1/4).
        expected = ((150.0**4 + 250.0**4 + 250.0**4) / 3.0) ** 0.25
        assert normalized_power(self.STREAM, window_samples=2) == pytest.approx(
            expected
        )

    def test_non_default_window_differs_from_default(self) -> None:
        ramp = [float(i) for i in range(60)]
        assert normalized_power(ramp, window_samples=60) != pytest.approx(
            normalized_power(ramp)
        )

    def test_window_larger_than_series_falls_back_to_mean(self) -> None:
        # Documented short-file semantics apply for any window length.
        assert normalized_power([100.0, 200.0], window_samples=5) == pytest.approx(
            150.0
        )

    def test_non_positive_window_raises(self) -> None:
        with pytest.raises(ValueError, match="window_samples"):
            normalized_power(self.STREAM, window_samples=0)

    def test_bike_power_load_passes_window_through(self) -> None:
        np_value = normalized_power(self.STREAM, window_samples=2)
        load = bike_power_load(self.STREAM, duration_s=4.0, ftp=200.0,
                               window_samples=2)
        assert load.normalized_power == pytest.approx(np_value)
        assert load.tss == pytest.approx(power_tss(4.0, np_value, 200.0))


class TestSelectLoadMethodConstantParameters:
    """The selection entry point forwards the engine constants."""

    THRESHOLDS = ThresholdBundle(lthr_bpm=169.0, hr_max_bpm=186.0, hr_rest_bpm=65.0)
    COEFFS = TrimpCoefficients.from_sex("male")

    def test_trimp_reference_minutes_flows_into_hrtss(self) -> None:
        # TRIMP is linear in duration, so a 30-minute reference exactly
        # doubles hrTSS relative to the documented 60-minute reference.
        activity = ActivityLoadInput(sport="Ride", duration_s=3600.0, hr_avg_bpm=150.0)
        default = select_load_method(activity, self.THRESHOLDS, coefficients=self.COEFFS)
        halved = select_load_method(
            activity,
            self.THRESHOLDS,
            coefficients=self.COEFFS,
            trimp_reference_minutes=30.0,
        )
        assert halved.tss == pytest.approx(2.0 * default.tss)

    def test_trimp_reference_minutes_reaches_the_reference_function(self) -> None:
        reference = trimp_at_lthr_reference(
            65.0, 186.0, 169.0, duration_min=30.0, coefficients=self.COEFFS
        )
        assert reference == pytest.approx(
            0.5
            * trimp_at_lthr_reference(65.0, 186.0, 169.0, coefficients=self.COEFFS)
        )

    def test_np_window_samples_flows_into_the_power_path(self) -> None:
        ramp = [float(i) for i in range(60)]
        activity = ActivityLoadInput(sport="Ride", duration_s=60.0, power_samples=ramp)
        thresholds = ThresholdBundle(ftp_watts=200.0)
        default = select_load_method(activity, thresholds, coefficients=self.COEFFS)
        wide = select_load_method(
            activity, thresholds, coefficients=self.COEFFS, np_window_samples=60
        )
        assert isinstance(default.detail, object)  # BikePowerLoad for "power"
        assert isinstance(wide.detail, BikePowerLoad)
        assert wide.tss == pytest.approx(
            power_tss(60.0, wide.detail.normalized_power, 200.0)
        )
        assert wide.tss != pytest.approx(default.tss)


class TestSrpeOwnerAnchorDocumented:
    """The sRPE factor 100/420 ≈ 0.2381 is the documented OWNER CHOICE
    (equivalent-effort anchor, 2026-10-05): one hour at RPE 7 (Foster AU =
    7 x 60 = 420) counts as one hour at threshold (100 TSS) — a documented
    owner decision, never presented as a pending calibration."""

    def test_srpe_load_docstring_documents_the_owner_anchor(self) -> None:
        doc = (inspect.getdoc(srpe_load) or "").lower()
        assert "owner" in doc
        assert "420" in doc
        assert "hrtss" in doc  # the rejected alternative is named

    def test_threshold_bundle_docstring_documents_the_owner_anchor(self) -> None:
        doc = (inspect.getdoc(ThresholdBundle) or "").lower()
        assert "owner" in doc
        assert "420" in doc

    def test_factor_one_gives_the_raw_foster_load(self) -> None:
        # Explicit factor 1.0 stays the raw Foster load: RPE 7 x 60 min = 420 AU.
        assert srpe_load(7.0, 60.0, tss_equivalent_factor=1.0) == pytest.approx(420.0)

    def test_engine_default_makes_one_hour_at_rpe_7_equal_100_tss(self) -> None:
        # Anchor arithmetic: 7 (RPE) x 60 (min) = 420 AU; 420 x (100/420)
        # = 100 TSS-equivalent — one hour at RPE 7 ≡ one hour at threshold.
        default_factor = ThresholdBundle().srpe_tss_equivalent_factor
        assert srpe_load(7.0, 60.0, tss_equivalent_factor=default_factor) == (
            pytest.approx(100.0)
        )

    def test_owner_gym_session_is_about_67_tss_equivalent(self) -> None:
        # The owner's real 40.3-minute RPE-7 gym session: 7 x 40.3 = 282.1 AU;
        # 282.1 x (100/420) = 67.1666... ≈ 67 TSS-equivalent (comparable to
        # the ~60 TSS of a one-hour ride; the old 1.0 factor made it 282).
        default_factor = ThresholdBundle().srpe_tss_equivalent_factor
        assert srpe_load(7.0, 40.3, tss_equivalent_factor=default_factor) == (
            pytest.approx(67.0, abs=0.5)
        )


class TestMinettiFlatCostStillDocumented:
    """Cr(0) = 3.6 stays the documented Minetti polynomial constant term."""

    def test_flat_cost_is_the_polynomial_constant_term(self) -> None:
        assert minetti_energy_cost(0.0) == pytest.approx(3.6)
