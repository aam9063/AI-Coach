"""Reference-value tests for HR-based load (Banister TRIMP / hrTSS) and strength sRPE.

Every expected value below is hand-derived from the formulas documented in
``app/engine/load.py`` (Banister 1991 TRIMP, hrTSS normalised to 1h at LTHR,
Foster et al. 2001 sRPE). Owner thresholds used throughout:

    HRrest = 65 bpm, HRmax = 186 bpm, LTHR = 169 bpm.

Hand arithmetic for the reference points:

    dHRr@LTHR = (169 - 65) / (186 - 65) = 104 / 121 = 0.8595041322
    dHRr@150  = (150 - 65) / (186 - 65) =  85 / 121 = 0.7024793388

    TRIMP_1h@LTHR (male)  = 60 * 0.8595041322 * 0.64 * e^(1.92 * 0.8595041322)
                          = 33.0049586777 * e^(1.6502479339)
                          = 33.0049586777 * 5.2082699857
                          = 171.8987682780
    TRIMP 60min@150 (male)  = 60 * 0.7024793388 * 0.64 * e^(1.92 * 0.7024793388)
                            = 26.9752066116 * e^(1.3487603306)
                            = 26.9752066116 * 3.8526460124
                            = 103.9259369844
    hrTSS 60min@150 (male)  = 103.9259369844 / 171.8987682780 * 100 = 60.4576391242

    TRIMP 60min@150 (female) = 60 * 0.7024793388 * 0.86 * e^(1.67 * 0.7024793388)
                             = 36.2479742456 * e^(1.1731404916)
                             = 36.2479742456 * 3.2320689587
                             = 117.1579329983
    TRIMP_1h@LTHR (female)   = 60 * 0.8595041322 * 0.86 * e^(1.67 * 0.8595041322)
                             = 44.3702134020 * e^(1.4353719079)
                             = 44.3702134020 * 4.2013544879
                             = 186.3252731696
    hrTSS 60min@150 (female) = 117.1579329983 / 186.3252731696 * 100 = 62.8781758938

    sRPE (Foster 2001): load = RPE * minutes; TSS-equivalent = load * factor.
    7 (CR-10) x 60 min = 420 AU; with factor 0.5 -> 210.0 TSS-equivalent.
"""

from typing import Final

import pytest

from app.engine.load import (
    TrimpCoefficients,
    hr_ratio,
    hrtss,
    srpe_load,
    trimp,
    trimp_at_lthr_reference,
)

HR_REST: Final = 65.0
HR_MAX: Final = 186.0
LTHR: Final = 169.0

# Banister 1991 exponent coefficients, male and female (selectable, overridable).
MALE: Final = TrimpCoefficients(a=0.64, b=1.92)
FEMALE: Final = TrimpCoefficients(a=0.86, b=1.67)

# Hand-derived reference values (see module docstring for the arithmetic).
TRIMP_1H_LTHR_MALE: Final = 171.8987682780
TRIMP_60MIN_AT_150_MALE: Final = 103.9259369844
HRTSS_60MIN_AT_150_MALE: Final = 60.4576391242
TRIMP_60MIN_AT_150_FEMALE: Final = 117.1579329983
TRIMP_1H_LTHR_FEMALE: Final = 186.3252731696
HRTSS_60MIN_AT_150_FEMALE: Final = 62.8781758938


class TestHrRatio:
    def test_ratio_at_threshold(self) -> None:
        # (169 - 65) / (186 - 65) = 104 / 121
        assert hr_ratio(LTHR, HR_REST, HR_MAX) == pytest.approx(104 / 121)

    def test_ratio_bounds_are_clamped(self) -> None:
        # Below resting HR clamps to 0.0; above HRmax clamps to 1.0 (data glitches).
        assert hr_ratio(HR_REST - 10.0, HR_REST, HR_MAX) == 0.0
        assert hr_ratio(HR_MAX + 5.0, HR_REST, HR_MAX) == 1.0

    def test_rejects_hr_max_not_above_rest(self) -> None:
        with pytest.raises(ValueError, match="HR max"):
            hr_ratio(150.0, HR_REST, HR_REST)  # HRmax == HRrest
        with pytest.raises(ValueError, match="HR max"):
            hr_ratio(150.0, HR_REST, HR_REST - 1.0)  # HRmax < HRrest


class TestTrimp:
    def test_male_reference_60min_at_150(self) -> None:
        assert trimp(60.0, 150.0, HR_REST, HR_MAX, coefficients=MALE) == pytest.approx(
            TRIMP_60MIN_AT_150_MALE, rel=1e-9
        )

    def test_female_reference_60min_at_150(self) -> None:
        assert trimp(
            60.0, 150.0, HR_REST, HR_MAX, coefficients=FEMALE
        ) == pytest.approx(TRIMP_60MIN_AT_150_FEMALE, rel=1e-9)

    def test_coefficients_are_explicit_and_overridable(self) -> None:
        # Custom a/b override both named sets: a=1.0, b=1.0 -> dHRr * e^dHRr * minutes.
        custom = TrimpCoefficients(a=1.0, b=1.0)
        # 30 * (85/121) * e^(85/121) = 20.8743901161 * 2.019207... = 42.15...
        expected = 30.0 * (85 / 121) * (2.718281828459045 ** (85 / 121))
        assert trimp(
            30.0, 150.0, HR_REST, HR_MAX, coefficients=custom
        ) == pytest.approx(expected, rel=1e-9)

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            trimp(0.0, 150.0, HR_REST, HR_MAX, coefficients=MALE)
        with pytest.raises(ValueError, match="duration"):
            trimp(-5.0, 150.0, HR_REST, HR_MAX, coefficients=MALE)

    def test_unknown_sex_is_rejected_not_defaulted(self) -> None:
        with pytest.raises(ValueError, match="sex"):
            TrimpCoefficients.from_sex("nonbinary")
        with pytest.raises(ValueError, match="sex"):
            TrimpCoefficients.from_sex("")


class TestTrimpAtLthrReference:
    def test_male_reference_one_hour(self) -> None:
        assert trimp_at_lthr_reference(
            HR_REST, HR_MAX, LTHR, coefficients=MALE
        ) == pytest.approx(TRIMP_1H_LTHR_MALE, rel=1e-9)

    def test_female_reference_one_hour(self) -> None:
        assert trimp_at_lthr_reference(
            HR_REST, HR_MAX, LTHR, coefficients=FEMALE
        ) == pytest.approx(TRIMP_1H_LTHR_FEMALE, rel=1e-9)

    def test_duration_scales_linearly(self) -> None:
        # TRIMP is linear in duration: 30 min at LTHR = half of 1h at LTHR.
        assert trimp_at_lthr_reference(
            HR_REST, HR_MAX, LTHR, duration_min=30.0, coefficients=MALE
        ) == pytest.approx(TRIMP_1H_LTHR_MALE / 2.0, rel=1e-9)


class TestHrtss:
    def test_male_reference_60min_at_150(self) -> None:
        assert hrtss(
            TRIMP_60MIN_AT_150_MALE, TRIMP_1H_LTHR_MALE
        ) == pytest.approx(HRTSS_60MIN_AT_150_MALE, rel=1e-9)

    def test_female_reference_60min_at_150(self) -> None:
        assert hrtss(
            TRIMP_60MIN_AT_150_FEMALE, TRIMP_1H_LTHR_FEMALE
        ) == pytest.approx(HRTSS_60MIN_AT_150_FEMALE, rel=1e-9)

    def test_reference_session_scores_exactly_100(self) -> None:
        # A 1h session at LTHR normalises to exactly 100 hrTSS.
        assert hrtss(TRIMP_1H_LTHR_MALE, TRIMP_1H_LTHR_MALE) == pytest.approx(100.0)


class TestSrpeLoad:
    def test_foster_load_times_factor(self) -> None:
        # 7 (CR-10) x 60 min = 420 AU; scaled by 0.5 -> 210 TSS-equivalent.
        assert srpe_load(7.0, 60.0, tss_equivalent_factor=0.5) == pytest.approx(210.0)

    def test_factor_one_is_raw_foster_load(self) -> None:
        assert srpe_load(7.0, 60.0, tss_equivalent_factor=1.0) == pytest.approx(420.0)

    def test_rejects_rpe_outside_cr10(self) -> None:
        with pytest.raises(ValueError, match="RPE"):
            srpe_load(-0.1, 60.0, tss_equivalent_factor=0.5)
        with pytest.raises(ValueError, match="RPE"):
            srpe_load(10.5, 60.0, tss_equivalent_factor=0.5)

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            srpe_load(7.0, 0.0, tss_equivalent_factor=0.5)
        with pytest.raises(ValueError, match="duration"):
            srpe_load(7.0, -1.0, tss_equivalent_factor=0.5)

    def test_rejects_negative_factor(self) -> None:
        with pytest.raises(ValueError, match="factor"):
            srpe_load(7.0, 60.0, tss_equivalent_factor=-0.5)


class TestPurity:
    """The engine stays pure (PROJECT_BRIEF sections 6/14): no I/O layer imports."""

    def test_engine_modules_import_no_io_layers_or_settings(self) -> None:
        import ast
        import importlib
        import inspect
        import pkgutil

        import app.engine

        banned = ("app.db", "app.ingest", "app.core")
        module_names = {
            info.name for info in pkgutil.iter_modules(app.engine.__path__)
        }
        assert {"load", "pmc"} <= module_names, (
            f"engine package contents changed unexpectedly: {sorted(module_names)}"
        )
        for name in sorted(module_names):
            module = importlib.import_module(f"app.engine.{name}")
            tree = ast.parse(inspect.getsource(module))
            imported = {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            imported |= {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            offenders = [n for n in imported if n.startswith(banned)]
            assert offenders == [], (
                f"engine purity violated by imports in app.engine.{name}: {offenders}"
            )
