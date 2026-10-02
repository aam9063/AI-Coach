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

Bike power-based load (Coggan; Allen & Coggan, "Training and Racing with
a Power Meter"):

    NP  = (mean over rolling 30 s windows of (window mean power)^4)^(1/4)
    IF  = NP / FTP
    TSS = (duration_s * NP * IF) / (FTP * 3600) * 100

The power stream is a per-second sample sequence (as produced by the
ingest parser), so the rolling window is 30 consecutive samples with a
1-sample step. Missing samples are ``None`` entries (the parser emits
``None`` for stream gaps). Documented missing-sample and short-file
semantics (see :func:`normalized_power` for details):

- Each 30 s window is averaged over its valid (non-``None``) samples, and
  a window qualifies only when ``valid_count / 30 >= min_valid_fraction``
  (default 1.0: only fully complete windows count; owners may relax the
  parameter explicitly for gap-heavy streams).
- A series shorter than the 30 s window falls back to NP = average of the
  valid samples (the smoothing window never fills, so NP degenerates to
  average power for short files).
- Zero valid samples, or a series of at least 30 samples with no
  qualifying window at the given ``min_valid_fraction``, raise
  ``ValueError`` instead of returning a silently degraded NP.

All functions are pure and fully typed; validation errors raise
``ValueError`` rather than clamping or silently defaulting, except the
documented clamping of ``dHRr`` to [0, 1].
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "BikePowerLoad",
    "TrimpCoefficients",
    "bike_power_load",
    "hr_ratio",
    "hrtss",
    "intensity_factor",
    "normalized_power",
    "power_tss",
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


NP_WINDOW_SAMPLES = 30
"""Rolling-window length for Normalized Power: 30 consecutive samples.

The power stream is per-second (1 Hz), so 30 samples = 30 s, per Coggan's
NP definition. Module-level constant so tests and callers can reference it.
"""


def _window_mean(window: Sequence[float | None]) -> float:
    """Mean of the valid (non-``None``) samples in a rolling window."""
    valid = [s for s in window if s is not None]
    return math.fsum(valid) / len(valid)


def normalized_power(
    power_samples: Sequence[float | None],
    *,
    min_valid_fraction: float = 1.0,
) -> float:
    """Coggan Normalized Power of a per-second power stream.

    Formula: NP = (mean over rolling 30 s windows of (window mean)^4)^(1/4),
    where windows are 30 consecutive samples (:data:`NP_WINDOW_SAMPLES`,
    1 Hz stream) advanced one sample at a time.

    Missing samples: ``None`` entries mark stream gaps. Each window's mean
    is computed over its valid (non-``None``) samples only; the window
    contributes to NP only when ``valid_count / 30 >= min_valid_fraction``.
    The default ``min_valid_fraction=1.0`` is strict (only fully complete
    windows count); pass e.g. ``0.9`` for gap-heavy streams. Values outside
    ``(0, 1]`` raise ``ValueError``.

    Short files: fewer samples than the 30 s window means the smoothing
    window never fills, so NP falls back to the average of the valid
    samples (NP degenerates to average power for short files).

    No silent nonsense: zero valid samples raises ``ValueError``
    ("no usable power data"), and a series of at least 30 samples with no
    qualifying window at the given ``min_valid_fraction`` raises
    ``ValueError`` ("no qualifying rolling window") rather than returning
    a degraded value — relax ``min_valid_fraction`` explicitly instead.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter".
    """
    if not 0.0 < min_valid_fraction <= 1.0:
        raise ValueError(
            f"min_valid_fraction must be within (0, 1], got {min_valid_fraction!r}"
        )
    samples = list(power_samples)
    if not any(s is not None for s in samples):
        raise ValueError(
            "no usable power data: samples sequence is empty or all missing"
        )
    if len(samples) < NP_WINDOW_SAMPLES:
        # Documented short-file fallback: NP -> average of valid samples.
        return _window_mean(samples)
    fourth_powers: list[float] = []
    for start in range(len(samples) - NP_WINDOW_SAMPLES + 1):
        window = samples[start : start + NP_WINDOW_SAMPLES]
        valid_count = sum(1 for s in window if s is not None)
        if valid_count / NP_WINDOW_SAMPLES < min_valid_fraction:
            continue  # window lacks enough valid samples: skipped
        fourth_powers.append(_window_mean(window) ** 4)
    if not fourth_powers:
        raise ValueError(
            "no qualifying rolling window: every 30 s window has fewer valid "
            f"samples than min_valid_fraction={min_valid_fraction!r} allows; "
            "relax min_valid_fraction or provide more complete data"
        )
    mean_fourth = math.fsum(fourth_powers) / len(fourth_powers)
    return math.pow(mean_fourth, 0.25)


def intensity_factor(np_value: float, ftp: float) -> float:
    """Coggan Intensity Factor IF = NP / FTP.

    Non-positive FTP (misconfigured threshold) or negative NP raise
    ``ValueError`` so a bad threshold can never silently produce infinite
    or negative intensity.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter".
    """
    if ftp <= 0.0:
        raise ValueError(f"FTP must be positive, got {ftp!r} watts")
    if np_value < 0.0:
        raise ValueError(f"NP must be non-negative, got {np_value!r} watts")
    return np_value / ftp


def power_tss(duration_s: float, np_value: float, ftp: float) -> float:
    """Coggan Training Stress Score for a bike session.

    Formula: TSS = (duration_s * NP * IF) / (FTP * 3600) * 100, with
    IF = NP / FTP. Equivalently TSS = 100 * (NP / FTP)^2 * hours, so one
    hour at NP = FTP scores exactly 100 TSS.

    Non-positive duration, negative NP, or non-positive FTP raise
    ``ValueError``.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter".
    """
    if duration_s <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_s!r} seconds")
    intensity = intensity_factor(np_value, ftp)
    return duration_s * np_value * intensity / (ftp * 3600.0) * 100.0


@dataclass(frozen=True, slots=True)
class BikePowerLoad:
    """Bike power-based load results consumed by method selection (LOAD-6).

    ``normalized_power`` in watts, ``intensity_factor`` dimensionless,
    ``tss`` on the Coggan TSS scale (1 h at FTP = 100).
    """

    normalized_power: float
    intensity_factor: float
    tss: float


def bike_power_load(
    power_samples: Sequence[float | None],
    *,
    duration_s: float,
    ftp: float,
) -> BikePowerLoad:
    """Convenience entry point: NP, IF and TSS for one bike session.

    Combines :func:`normalized_power` (missing-sample and short-file
    semantics documented there), :func:`intensity_factor` and
    :func:`power_tss`. ``duration_s`` is the session wall-clock duration in
    seconds (explicit, because a gapped per-second stream has fewer valid
    samples than seconds elapsed); non-positive durations raise
    ``ValueError``. Returns a :class:`BikePowerLoad` for downstream method
    selection.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter".
    """
    if duration_s <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_s!r} seconds")
    np_value = normalized_power(power_samples)
    if_value = intensity_factor(np_value, ftp)
    return BikePowerLoad(
        normalized_power=np_value,
        intensity_factor=if_value,
        tss=power_tss(duration_s, np_value, ftp),
    )
