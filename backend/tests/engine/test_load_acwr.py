"""Reference-value and metadata-shape tests for EWMA ACWR (ODD LOAD-8).

Formula (EWMA version, Williams et al. 2017 "How much does too much
exercise affect cognition?", or rather the ACWR-EWMA paper: Williams,
Trewartha, Cross, Kemp & Stokes 2017): acute and chronic loads are
exponentially weighted moving averages of the daily load,

    EWMA_t = EWMA_{t-1} + (TSS_t - EWMA_{t-1}) * (1 - e^(-1/tau))

with tau_acute = 7 days and tau_chronic = 28 days, and

    ACWR = acute_EWMA / chronic_EWMA.

Hand-derived reference values (seed 0, constant daily TSS of 100; with the
closed form EWMA_n = 100 * (1 - e^(-n/tau))):

    single day: acute  = 100 * (1 - e^(-1/7))  = 13.3122100250
                chronic = 100 * (1 - e^(-1/28)) = 3.5084055628
                ratio   = 13.3122100250 / 3.5084055628 = 3.7943760454
    after 300 days: acute ~ 100, chronic = 100 * (1 - e^(-300/28))
                    = 99.9977774843, ratio = 1.0000222257 ~ 1.0.

Zero-chronic semantics under test: an all-zero (or seed-only) load history
gives chronic_EWMA == 0 and ``ratio is None`` — a defined "no chronic
baseline yet" state, never 0 and never an exception (documented in
``app/engine/pmc.py``).

Context-only stance under test (PROJECT_BRIEF section 7.2): the result is
plain metadata. The returned type carries exactly the fields
``acute_ewma``, ``chronic_ewma`` and ``ratio`` — no ``warning``, ``risk``
or ``flag`` field of any kind. ACWR must never trigger warnings on its
own; warning logic belongs to Feature 5 (section 7.4).
"""

import dataclasses
import datetime as dt
from typing import Final

import pytest

from app.engine.pmc import (
    ACWR_TAU_ACUTE_DAYS,
    ACWR_TAU_CHRONIC_DAYS,
    AcwrResult,
    acwr_ewma,
)

D0: Final = dt.date(2026, 1, 1)

ACUTE_DAY1: Final = 13.3122100250
CHRONIC_DAY1: Final = 3.5084055628
ACWR_DAY1: Final = 3.7943760454
CHRONIC_300D: Final = 99.9977774843


class TestReferenceValues:
    def test_defaults_are_7_and_28_days(self) -> None:
        assert ACWR_TAU_ACUTE_DAYS == 7.0
        assert ACWR_TAU_CHRONIC_DAYS == 28.0

    def test_single_day_ratio(self) -> None:
        result = acwr_ewma({D0: 100.0})
        assert result.acute_ewma == pytest.approx(ACUTE_DAY1, abs=1e-9)
        assert result.chronic_ewma == pytest.approx(CHRONIC_DAY1, abs=1e-9)
        assert result.ratio == pytest.approx(ACWR_DAY1, abs=1e-9)

    def test_constant_load_converges_to_ratio_one(self) -> None:
        loads = {D0 + dt.timedelta(days=i): 100.0 for i in range(300)}
        result = acwr_ewma(loads)
        assert result.acute_ewma == pytest.approx(100.0, abs=1e-6)
        assert result.chronic_ewma == pytest.approx(CHRONIC_300D, abs=1e-9)
        assert result.ratio == pytest.approx(1.0, abs=1e-3)

    def test_time_constants_are_configurable(self) -> None:
        result = acwr_ewma({D0: 100.0}, tau_acute_days=3.0, tau_chronic_days=21.0)
        acute = 100.0 * (1.0 - pow(2.718281828459045, -1.0 / 3.0))
        chronic = 100.0 * (1.0 - pow(2.718281828459045, -1.0 / 21.0))
        assert result.acute_ewma == pytest.approx(acute, rel=1e-9)
        assert result.chronic_ewma == pytest.approx(chronic, rel=1e-9)

    def test_seed_is_explicit(self) -> None:
        result = acwr_ewma({D0: 100.0}, seed_acute=70.0, seed_chronic=70.0)
        assert result.acute_ewma == pytest.approx(
            70.0 + 30.0 * (1.0 - pow(2.718281828459045, -1.0 / 7.0)), rel=1e-9
        )
        assert result.chronic_ewma == pytest.approx(
            70.0 + 30.0 * (1.0 - pow(2.718281828459045, -1.0 / 28.0)), rel=1e-9
        )


class TestZeroChronicSemantics:
    def test_all_zero_history_gives_ratio_none(self) -> None:
        loads = {D0 + dt.timedelta(days=i): 0.0 for i in range(5)}
        result = acwr_ewma(loads)
        assert result.chronic_ewma == 0.0
        assert result.acute_ewma == 0.0
        assert result.ratio is None

    def test_ratio_none_is_documented_not_zero(self) -> None:
        # A missing baseline must never surface as ratio 0.0 or raise.
        result = acwr_ewma({D0: 0.0})
        assert result.ratio is None


class TestContextOnlyMetadata:
    def test_result_type_has_exactly_the_metadata_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(AcwrResult)}
        assert field_names == {"acute_ewma", "chronic_ewma", "ratio"}

    def test_result_carries_no_warning_risk_or_flag_field(self) -> None:
        field_names = {f.name for f in dataclasses.fields(AcwrResult)}
        assert not any(
            token in name for name in field_names for token in ("warn", "risk", "flag")
        )
        result = acwr_ewma({D0: 100.0})
        # slots dataclasses have no __dict__, so iterate declared fields.
        for field in dataclasses.fields(result):
            assert "warn" not in field.name and "risk" not in field.name and "flag" not in field.name  # noqa: E501
            value = getattr(result, field.name)
            assert value is None or isinstance(value, float)


class TestDailySeriesValidation:
    def test_missing_calendar_day_raises(self) -> None:
        loads = {D0: 100.0, D0 + dt.timedelta(days=2): 100.0}
        with pytest.raises(ValueError, match="gap"):
            acwr_ewma(loads)

    def test_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            acwr_ewma({})

    def test_negative_tss_raises(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            acwr_ewma({D0: -1.0})

    def test_non_positive_time_constant_raises(self) -> None:
        with pytest.raises(ValueError, match="tau"):
            acwr_ewma({D0: 100.0}, tau_chronic_days=0.0)
