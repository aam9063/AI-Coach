"""Heart-rate-based load: Banister TRIMP, hrTSS, and strength sRPE.

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core.settings`` (PROJECT_BRIEF sections 6 and 14). Thresholds are
explicit parameters; side effects and settings reads belong in callers.

Formulas and references
-----------------------
Heart-rate ratio (training intensity fraction, Banister 1991):

    dHRr = (HRavg - HRrest) / (HRmax - HRrest)

clamped to [0, 1]; ``HRmax <= HRrest`` is rejected with ``ValueError``.

Banister TRIMP (training impulse, Banister 1991 "Modeling elite athletic
performance"):

    TRIMP = duration_min * dHRr * a * e^(b * dHRr)

with the sex-specific exponent coefficients

    male:   a = 0.64, b = 1.92
    female: a = 0.86, b = 1.67

Both coefficient sets are selectable via :meth:`TrimpCoefficients.from_sex`
and overridable by constructing :class:`TrimpCoefficients` explicitly.
Unknown sex strings are rejected, never silently defaulted.

hrTSS (heart-rate Training Stress Score, normalised to one hour at LTHR so
that 1h at threshold = 100 hrTSS):

    TRIMP_ref = TRIMP(60 min, HRavg = LTHR)
    hrTSS     = TRIMP / TRIMP_ref * 100

Strength sRPE (session-RPE method, Foster et al. 2001 "A new approach to
monitoring exercise training", J Strength Cond Res 15(1):109-115):

    Foster load        = RPE(CR-10) * minutes
    TSS-equivalent     = Foster load * tss_equivalent_factor

The ``tss_equivalent_factor`` maps Foster arbitrary units (AU) onto the
TSS scale; it is an explicit, caller-supplied parameter (engine constants
are owned by LOAD-11), documented here as the single scaling knob.

All functions are pure and fully typed; validation errors raise
``ValueError`` rather than clamping or silently defaulting, except the
documented clamping of ``dHRr`` to [0, 1].
"""

import math
from dataclasses import dataclass

__all__ = [
    "TrimpCoefficients",
    "hr_ratio",
    "hrtss",
    "srpe_load",
    "trimp",
    "trimp_at_lthr_reference",
]


@dataclass(frozen=True, slots=True)
class TrimpCoefficients:
    """Banister TRIMP exponent coefficients (a, b) for ``a * e^(b * dHRr)``.

    Named sets: male a=0.64/b=1.92, female a=0.86/b=1.67 (Banister 1991).
    Construct directly to override; unknown sex strings raise ``ValueError``
    instead of defaulting.
    """

    a: float
    b: float

    @classmethod
    def from_sex(cls, sex: str) -> "TrimpCoefficients":
        """Return the Banister 1991 coefficients for ``"male"`` or ``"female"``."""
        normalized = sex.strip().lower()
        if normalized == "male":
            return cls(a=0.64, b=1.92)
        if normalized == "female":
            return cls(a=0.86, b=1.67)
        raise ValueError(
            f"unknown sex {sex!r}: expected 'male' or 'female' "
            "(or construct TrimpCoefficients explicitly)"
        )


def hr_ratio(hr_avg_bpm: float, hr_rest_bpm: float, hr_max_bpm: float) -> float:
    """Heart-rate intensity fraction dHRr = (HRavg - HRrest) / (HRmax - HRrest).

    Clamped to [0, 1]: average HR below resting HR yields 0.0 and above
    HRmax yields 1.0 (sensor glitches must not inflate load). Raises
    ``ValueError`` when ``HRmax <= HRrest`` (degenerate range).

    Reference: Banister 1991.
    """
    if hr_max_bpm <= hr_rest_bpm:
        raise ValueError(
            f"HR max ({hr_max_bpm!r} bpm) must be strictly greater than "
            f"HR rest ({hr_rest_bpm!r} bpm)"
        )
    ratio = (hr_avg_bpm - hr_rest_bpm) / (hr_max_bpm - hr_rest_bpm)
    return min(1.0, max(0.0, ratio))


def _validate_duration(duration_min: float) -> float:
    if duration_min <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_min!r} minutes")
    return duration_min


def trimp(
    duration_min: float,
    hr_avg_bpm: float,
    hr_rest_bpm: float,
    hr_max_bpm: float,
    *,
    coefficients: TrimpCoefficients,
) -> float:
    """Banister TRIMP = duration_min * dHRr * a * e^(b * dHRr).

    ``dHRr`` is :func:`hr_ratio` (clamped to [0, 1]; ``HRmax <= HRrest``
    raises ``ValueError``). Non-positive duration raises ``ValueError``.
    Coefficients are explicit (:class:`TrimpCoefficients`).

    Reference: Banister 1991.
    """
    duration = _validate_duration(duration_min)
    ratio = hr_ratio(hr_avg_bpm, hr_rest_bpm, hr_max_bpm)
    return duration * ratio * coefficients.a * math.exp(coefficients.b * ratio)


def trimp_at_lthr_reference(
    hr_rest_bpm: float,
    hr_max_bpm: float,
    lthr_bpm: float,
    *,
    duration_min: float = 60.0,
    coefficients: TrimpCoefficients,
) -> float:
    """TRIMP of ``duration_min`` (default 60) at LTHR: the hrTSS normaliser.

    Computed with the same :func:`trimp` formula at ``HRavg = LTHR``.
    Reference: Banister 1991; normalisation convention per hrTSS
    (1h at threshold = 100 hrTSS).
    """
    return trimp(
        duration_min,
        lthr_bpm,
        hr_rest_bpm,
        hr_max_bpm,
        coefficients=coefficients,
    )


def hrtss(trimp_value: float, trimp_reference: float) -> float:
    """hrTSS = TRIMP / TRIMP_1h@LTHR * 100 (1h at threshold = 100).

    Raises ``ValueError`` for a non-positive reference so a misconfigured
    threshold can never silently produce infinite/negative scores.
    """
    if trimp_reference <= 0.0:
        raise ValueError(
            f"trimp_reference must be positive, got {trimp_reference!r}"
        )
    return trimp_value / trimp_reference * 100.0


def srpe_load(rpe: float, duration_min: float, *, tss_equivalent_factor: float) -> float:
    """Strength session load via session-RPE: RPE(CR-10) * minutes * factor.

    ``tss_equivalent_factor`` scales Foster arbitrary units (AU) onto the
    TSS scale; pass 1.0 for the raw Foster load. RPE outside [0, 10],
    non-positive duration, or a negative factor raise ``ValueError``.

    Reference: Foster et al. 2001.
    """
    if not 0.0 <= rpe <= 10.0:
        raise ValueError(f"RPE must be within the CR-10 scale [0, 10], got {rpe!r}")
    duration = _validate_duration(duration_min)
    if tss_equivalent_factor < 0.0:
        raise ValueError(
            f"tss_equivalent_factor must be non-negative, got {tss_equivalent_factor!r}"
        )
    return rpe * duration * tss_equivalent_factor
