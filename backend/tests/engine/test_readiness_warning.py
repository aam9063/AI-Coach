"""Multi-signal warning rule and structured readiness assessment (RID-3 and
RID-4, PROJECT_BRIEF section 7.4).

Contract under test (``app.engine.readiness.readiness_assessment`` /
``ReadinessAssessment``):

- The rule is EXACTLY "two or more adverse signals agree" (section 7.4 and
  the section 12.5 acceptance criterion): a single adverse signal alone
  must NEVER produce a reduce-intensity suggestion, for every adverse
  signal type.
- Adverse directions (the interpretation layer for the value-direction
  vocabulary of the RID-2 signals):

    ================  ==========================  ==========================
    Signal            Adverse when                Reference
    ================  ==========================  ==========================
    hrv_ln_rmssd      direction "low"             Plews et al. 2013
                      (suppressed rolling mean)   (classic overreach mark)
    resting_hr        direction "elevated"        section 7.4
    sleep_duration    direction "low" (short)     section 7.4
    tsb               below the configurable      OWNER CHOICE (-10.0; see
                      "very negative" threshold   readiness.py provenance)
    subjective        fatigue reported            section 7.4 example
    fatigue
    ================  ==========================  ==========================

  The opposite directions (elevated HRV, low resting HR, long sleep) are
  NOT adverse: the direction describes the VALUE, not the advice.
- A signal with ``status == "insufficient_data"`` must NOT count as
  agreeing: absence of evidence is not evidence of an adverse signal.
- ACWR is context-only (section 7.2, Impellizzeri et al. 2020): changing
  it — including a very high value — must never change the verdict.
- The output is a structured object (RID-4): every signal, the adverse
  keys, the agreement count, an explicit ``suggest_reduce_intensity``
  boolean and human-readable reasons. NO composite score, NO single
  readiness number (section 7.4).
- The suggestion is a "consider reducing intensity" statement, never a
  diagnosis or medical advice (section 3).
- The result is trivially convertible to a typed Pydantic shape (all
  fields primitive or tuples of frozen dataclasses); the engine itself
  never imports pydantic.
"""

import dataclasses
import datetime as dt
import math
from collections.abc import Sequence

import pytest

from app.engine.readiness import (
    ReadinessAssessment,
    ReadinessSignal,
    hrv_readiness,
    readiness_assessment,
    resting_hr_readiness,
    sleep_readiness,
)

_SERIES_START = dt.date(2026, 6, 1)

# HRV: 60-day baseline alternating 3.9 / 4.1 (mean 4.0, SD ~0.1008), then a
# constant 7-day window. Window 3.9 flags "low", 4.1 flags "elevated",
# 4.0 is "normal".
_HRV_BASELINE: list[float | None] = [3.9, 4.1] * 30
_HRV_LOW = 3.9
_HRV_NORMAL = 4.0
_HRV_HIGH = 4.1

# Resting HR: 30-day baseline alternating 55 / 65 (mean 60, SD ~5.085).
# As-of 65 flags "elevated", 55 flags "low", 60 is "normal".
_RHR_BASELINE: list[float | None] = [55.0, 65.0] * 15
_RHR_HIGH = 65.0
_RHR_NORMAL = 60.0
_RHR_LOW = 55.0

# Sleep: 30-day baseline alternating 7 / 8 h (mean 7.5, SD ~0.509).
# As-of 7.0 flags "low" (short), 8.0 flags "elevated", 7.5 is "normal".
_SLEEP_BASELINE: list[float | None] = [7.0, 8.0] * 15
_SLEEP_LOW = 7.0
_SLEEP_NORMAL = 7.5
_SLEEP_HIGH = 8.0

_TSB_FINE = 5.0
_TSB_VERY_NEGATIVE = -12.0  # below the default -10.0 threshold


def _daily(values: Sequence[float | None]) -> dict[dt.date, float | None]:
    return {
        _SERIES_START + dt.timedelta(days=i): value
        for i, value in enumerate(values)
    }


def _hrv(window_value: float) -> ReadinessSignal:
    return hrv_readiness(_daily([*_HRV_BASELINE, *[window_value] * 7]))


def _hrv_insufficient() -> ReadinessSignal:
    # A None inside the trailing 7-day window -> explicit insufficient_data.
    return hrv_readiness(_daily([*_HRV_BASELINE, _HRV_NORMAL, _HRV_NORMAL,
                                 _HRV_NORMAL, _HRV_NORMAL, _HRV_NORMAL,
                                 _HRV_NORMAL, None]))


def _rhr(as_of: float) -> ReadinessSignal:
    return resting_hr_readiness(_daily([*_RHR_BASELINE, as_of]))


def _rhr_insufficient() -> ReadinessSignal:
    return resting_hr_readiness(_daily([*_RHR_BASELINE, None]))


def _sleep(as_of: float) -> ReadinessSignal:
    return sleep_readiness(_daily([*_SLEEP_BASELINE, as_of]))


def _all_normal() -> list[ReadinessSignal]:
    return [_hrv(_HRV_NORMAL), _rhr(_RHR_NORMAL), _sleep(_SLEEP_NORMAL)]


# ---------------------------------------------------------------------------
# RID-3: the rule fires only when two or more adverse signals agree
# ---------------------------------------------------------------------------


class TestSingleSignalNeverWarns:
    """REQUIRED negative cases: each single adverse signal alone does NOT warn."""

    def test_single_hrv_low_does_not_warn(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_NORMAL), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False
        assert result.suggestion is None
        assert result.adverse_signal_keys == ("hrv_ln_rmssd",)
        # It still NAMES the single signal that was found (evidence, not advice).
        assert len(result.reasons) == 1
        assert "HRV" in result.reasons[0]

    def test_single_resting_hr_elevated_does_not_warn(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_NORMAL), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False
        assert result.adverse_signal_keys == ("resting_hr",)

    def test_single_sleep_short_does_not_warn(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_NORMAL), _rhr(_RHR_NORMAL), _sleep(_SLEEP_LOW)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False
        assert result.adverse_signal_keys == ("sleep_duration",)

    def test_single_tsb_very_negative_does_not_warn(self) -> None:
        result = readiness_assessment(_all_normal(), tsb=_TSB_VERY_NEGATIVE)
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False
        assert result.adverse_signal_keys == ("tsb_very_negative",)

    def test_single_subjective_fatigue_does_not_warn(self) -> None:
        result = readiness_assessment(
            _all_normal(), tsb=_TSB_FINE, subjective_fatigue_reported=True
        )
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False
        assert result.adverse_signal_keys == ("subjective_fatigue",)


class TestOppositeDirectionsAreNotAdverse:
    """Direction semantics: the direction describes the VALUE, not the advice."""

    def test_hrv_elevated_is_not_adverse(self) -> None:
        # Elevated HRV can mark recovery above baseline — never a warning.
        result = readiness_assessment(
            [_hrv(_HRV_HIGH), _rhr(_RHR_NORMAL), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False

    def test_resting_hr_low_is_not_adverse(self) -> None:
        # A LOW resting HR is typically good (fitness), not a warning.
        result = readiness_assessment(
            [_hrv(_HRV_NORMAL), _rhr(_RHR_LOW), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False

    def test_sleep_long_is_not_adverse(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_NORMAL), _rhr(_RHR_NORMAL), _sleep(_SLEEP_HIGH)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False


class TestZeroAdverseSignals:
    def test_zero_adverse_signals_does_not_warn(self) -> None:
        result = readiness_assessment(
            _all_normal(), tsb=_TSB_FINE, subjective_fatigue_reported=False
        )
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False
        assert result.adverse_signal_keys == ()
        assert result.reasons == ()
        assert result.suggestion is None


class TestTwoOrMoreSignalsWarn:
    """REQUIRED positive cases: exactly two agreeing signals DO warn; more warn."""

    def test_exactly_two_readiness_signals_warn(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 2
        assert result.suggest_reduce_intensity is True
        assert result.adverse_signal_keys == ("hrv_ln_rmssd", "resting_hr")
        assert result.suggestion is not None
        assert "consider reducing intensity" in result.suggestion.lower()

    def test_exactly_two_context_signals_warn(self) -> None:
        result = readiness_assessment(
            _all_normal(), tsb=_TSB_VERY_NEGATIVE, subjective_fatigue_reported=True
        )
        assert result.agreement_count == 2
        assert result.suggest_reduce_intensity is True
        assert result.adverse_signal_keys == ("tsb_very_negative", "subjective_fatigue")

    def test_mixed_pair_of_readiness_and_tsb_warns(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_NORMAL), _rhr(_RHR_NORMAL), _sleep(_SLEEP_LOW)],
            tsb=_TSB_VERY_NEGATIVE,
        )
        assert result.agreement_count == 2
        assert result.suggest_reduce_intensity is True
        assert result.adverse_signal_keys == ("sleep_duration", "tsb_very_negative")

    def test_three_signals_warn(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_VERY_NEGATIVE,
        )
        assert result.agreement_count == 3
        assert result.suggest_reduce_intensity is True

    def test_all_five_adverse_warn_with_rest_suggestion(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_LOW)],
            tsb=_TSB_VERY_NEGATIVE,
            subjective_fatigue_reported=True,
        )
        assert result.agreement_count == 5
        assert result.suggest_reduce_intensity is True
        assert result.adverse_signal_keys == (
            "hrv_ln_rmssd",
            "resting_hr",
            "sleep_duration",
            "tsb_very_negative",
            "subjective_fatigue",
        )
        assert result.suggestion is not None
        suggestion = result.suggestion.lower()
        assert "consider reducing intensity" in suggestion
        # Still a suggestion, never an order or a diagnosis (section 3).
        assert "rest" in suggestion or "recovery" in suggestion

    def test_assessment_names_which_signals_agreed_and_why(self) -> None:
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_VERY_NEGATIVE,
        )
        assert len(result.reasons) == 3
        joined = " | ".join(result.reasons)
        assert "HRV" in joined
        assert "resting heart rate" in joined.lower()
        assert "TSB" in joined
        # Each reason points at its own signal, in the stable key order.
        assert "hrv_ln_rmssd" not in result.reasons[1]
        assert "resting_hr" not in result.reasons[0]


# ---------------------------------------------------------------------------
# insufficient_data must NOT count as agreement
# ---------------------------------------------------------------------------


class TestInsufficientDataNeverAgrees:
    def test_insufficient_signal_plus_one_adverse_does_not_warn(self) -> None:
        # HRV cannot be assessed; the one ASSESSED adverse signal is alone:
        # absence of evidence is not evidence, so no warning fires.
        result = readiness_assessment(
            [_hrv_insufficient(), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.adverse_signal_keys == ("resting_hr",)
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False

    def test_two_insufficient_signals_do_not_agree_with_each_other(self) -> None:
        result = readiness_assessment(
            [_hrv_insufficient(), _rhr_insufficient(), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.adverse_signal_keys == ()
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False

    def test_insufficient_hrv_alone_reports_no_agreement(self) -> None:
        result = readiness_assessment(
            [_hrv_insufficient(), _rhr(_RHR_NORMAL), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
        )
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False
        assert result.signals[0].status == "insufficient_data"


# ---------------------------------------------------------------------------
# ACWR is context-only (section 7.2) and must never influence the verdict
# ---------------------------------------------------------------------------


class TestAcwrIsContextOnly:
    @pytest.mark.parametrize("acwr", [None, 0.8, 1.3, 1.9])
    def test_acwr_never_changes_a_quiet_verdict(self, acwr: float | None) -> None:
        result = readiness_assessment(_all_normal(), tsb=_TSB_FINE, acwr=acwr)
        assert result.agreement_count == 0
        assert result.suggest_reduce_intensity is False
        assert result.adverse_signal_keys == ()

    @pytest.mark.parametrize("acwr", [None, 0.8, 9.9])
    def test_acwr_never_changes_a_warning_verdict(self, acwr: float | None) -> None:
        # Even a very high (or missing) ACWR cannot add to, or remove from,
        # the agreement count: two adverse signals stay exactly two.
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)],
            tsb=_TSB_FINE,
            acwr=acwr,
        )
        assert result.agreement_count == 2
        assert result.suggest_reduce_intensity is True
        assert result.adverse_signal_keys == ("hrv_ln_rmssd", "resting_hr")

    def test_acwr_is_carried_as_reported_context(self) -> None:
        result = readiness_assessment(_all_normal(), tsb=_TSB_FINE, acwr=1.42)
        assert result.acwr == pytest.approx(1.42)
        assert readiness_assessment(_all_normal(), tsb=_TSB_FINE).acwr is None


# ---------------------------------------------------------------------------
# TSB "very negative": configurable threshold with tolerant boundary
# ---------------------------------------------------------------------------


class TestTsbVeryNegative:
    def test_default_threshold_is_documented_owner_choice(self) -> None:
        from app.engine.readiness import DEFAULT_TSB_VERY_NEGATIVE

        assert DEFAULT_TSB_VERY_NEGATIVE == -10.0

    def test_tsb_below_threshold_is_adverse(self) -> None:
        result = readiness_assessment(_all_normal(), tsb=-10.5)
        assert result.adverse_signal_keys == ("tsb_very_negative",)
        assert result.agreement_count == 1
        assert result.suggest_reduce_intensity is False

    def test_tsb_exactly_at_threshold_is_not_adverse(self) -> None:
        # Strict "below the threshold": exactly -10.0 does NOT count
        # (tolerant boundary comparison, the ZON-9 margin precedent).
        result = readiness_assessment(_all_normal(), tsb=-10.0)
        assert result.adverse_signal_keys == ()
        assert result.agreement_count == 0

    def test_custom_threshold_changes_the_verdict(self) -> None:
        signals = _all_normal()
        lax = readiness_assessment(signals, tsb=-6.0, tsb_very_negative_below=-5.0)
        assert lax.adverse_signal_keys == ("tsb_very_negative",)
        strict = readiness_assessment(signals, tsb=-6.0)
        assert strict.adverse_signal_keys == ()
        # And the threshold is echoed back for audit.
        assert lax.tsb_very_negative_below == pytest.approx(-5.0)
        assert strict.tsb_very_negative_below == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# Validation: explicit ValueErrors, never silent degradation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_signal_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            readiness_assessment([_hrv(_HRV_NORMAL), _rhr(_RHR_NORMAL)], tsb=0.0)

    def test_duplicate_signal_key_raises(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            readiness_assessment(
                [_hrv(_HRV_NORMAL), _hrv(_HRV_NORMAL), _rhr(_RHR_NORMAL),
                 _sleep(_SLEEP_NORMAL)],
                tsb=0.0,
            )

    def test_unknown_signal_key_raises(self) -> None:
        stranger = dataclasses.replace(_hrv(_HRV_NORMAL), key="mood")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="unknown"):
            readiness_assessment([stranger, _rhr(_RHR_NORMAL), _sleep(_SLEEP_NORMAL)],
                                 tsb=0.0)

    def test_non_finite_tsb_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            readiness_assessment(_all_normal(), tsb=math.inf)

    def test_non_finite_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            readiness_assessment(_all_normal(), tsb=0.0,
                                 tsb_very_negative_below=math.nan)

    def test_non_finite_acwr_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            readiness_assessment(_all_normal(), tsb=0.0, acwr=math.nan)


# ---------------------------------------------------------------------------
# RID-4: the structured output object
# ---------------------------------------------------------------------------


class TestStructuredOutput:
    def test_every_signal_is_carried_with_direction_and_confidence(self) -> None:
        signals = [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_NORMAL)]
        result = readiness_assessment(signals, tsb=_TSB_VERY_NEGATIVE)
        assert result.signals == tuple(signals)
        assert [s.direction for s in result.signals] == ["low", "elevated", "normal"]
        assert all(0.0 < s.confidence <= 1.0 for s in result.signals)

    def test_result_is_frozen(self) -> None:
        result = readiness_assessment(_all_normal(), tsb=0.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.agreement_count = 7  # type: ignore[misc]

    def test_no_composite_score_anywhere(self) -> None:
        # Section 7.4: readiness is a structured object, never a single magic
        # score. No field of the assessment (or of any carried signal) may be
        # score-like.
        assessment_fields = {f.name for f in dataclasses.fields(ReadinessAssessment)}
        signal_fields = {f.name for f in dataclasses.fields(ReadinessSignal)}
        for name in assessment_fields | signal_fields:
            assert "score" not in name.lower(), name
        payload = dataclasses.asdict(
            readiness_assessment(
                [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_LOW)],
                tsb=_TSB_VERY_NEGATIVE,
                subjective_fatigue_reported=True,
            )
        )

        def _no_score(value: object) -> bool:
            if isinstance(value, dict):
                return all(
                    isinstance(k, str) and "score" not in k.lower() and _no_score(v)
                    for k, v in value.items()
                )
            if isinstance(value, (list, tuple)):
                return all(_no_score(v) for v in value)
            return True

        assert _no_score(payload)

    def test_context_inputs_are_reported(self) -> None:
        result = readiness_assessment(
            _all_normal(),
            tsb=_TSB_VERY_NEGATIVE,
            subjective_fatigue_reported=True,
            acwr=1.1,
        )
        assert result.tsb == pytest.approx(_TSB_VERY_NEGATIVE)
        assert result.subjective_fatigue_reported is True
        assert result.acwr == pytest.approx(1.1)
        assert result.tsb_very_negative_below == pytest.approx(-10.0)

    def test_pydantic_ready_shape_without_pydantic(self) -> None:
        # The engine must not import pydantic; the shape must be trivially
        # convertible instead: asdict() yields only primitives, lists and
        # str-keyed dicts, which maps 1:1 onto nested pydantic models.
        import app.engine.readiness as readiness_module

        assert "pydantic" not in vars(readiness_module)
        payload = dataclasses.asdict(
            readiness_assessment(_all_normal(), tsb=0.0, acwr=1.0)
        )

        def _primitive(value: object) -> bool:
            if value is None or isinstance(value, (str, bool, int, float)):
                return True
            if isinstance(value, (list, tuple)):
                return all(_primitive(v) for v in value)
            if isinstance(value, dict):
                return all(
                    isinstance(k, str) and _primitive(v) for k, v in value.items()
                )
            return False

        assert _primitive(payload)

    def test_suggestion_is_never_a_diagnosis_or_medical_advice(self) -> None:
        # Section 3: the agent never diagnoses. The engine output must not
        # carry diagnostic or medical conclusion language at all.
        forbidden = (
            "diagnos", "medical", "doctor", "physio", "illness", "injur",
            "disease", "overtrain", "syndrome", "patholog",
        )
        result = readiness_assessment(
            [_hrv(_HRV_LOW), _rhr(_RHR_HIGH), _sleep(_SLEEP_LOW)],
            tsb=_TSB_VERY_NEGATIVE,
            subjective_fatigue_reported=True,
        )
        text = (result.suggestion or "") + " | ".join(result.reasons)
        lowered = text.lower()
        for word in forbidden:
            assert word not in lowered, f"forbidden language: {word!r}"
        assert result.suggestion is not None
        assert "consider reducing intensity" in result.suggestion.lower()
