"""Training-load primitives: TRIMP/hrTSS, sRPE, bike power TSS, run rTSS, swim sTSS.

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

Run pace-based load (rTSS, Coggan-style normalisation to threshold run
speed, with the Minetti et al. 2002 grade-adjustment model):

    Cr(i)  = 155.4 i^5 - 30.4 i^4 - 43.3 i^3 + 46.3 i^2 + 19.5 i + 3.6
             (energy cost of running, J kg^-1 m^-1, at grade i =
             elevation gain / horizontal distance; Minetti et al. 2002,
             "Energy cost of walking and running at extreme uphill and
             downhill slopes", J Appl Physiol 93(3):1039-1046)
    NGS    = mean over valid speed samples of v * Cr(i) / Cr(0),
             with Cr(0) = 3.6 (normalized graded speed, m/s)
    IF     = NGS / threshold_run_speed
    rTSS   = duration_h * IF^2 * 100

Documented semantics (see :func:`normalized_graded_speed` for details):

- The grade of sample k is ``(alt[k] - alt[k-1]) / (dist[k] - dist[k-1])``
  from the per-second altitude (m) and cumulative distance (m) streams.
- Where the grade is unavailable it is treated as 0 (flat): sample k = 0,
  a missing altitude/distance sample, a non-positive distance delta (GPS
  noise), or an altitude/distance stream that is absent entirely. Grade
  0 is the neutral element of the Minetti model (``Cr(0) = 3.6``, so the
  adjusted speed equals the measured speed), never an assumption that the
  terrain was flat.
- Missing speed samples (``None``, the parser's stream-gap marker) are
  excluded from the mean, mirroring the power-stream gap semantics.
- The Minetti polynomial is fitted for grades in about [-0.20, +0.40];
  grades outside that domain are extrapolations and are NOT clamped (no
  silent correction of sensor data).
- ``threshold_run_speed`` is a required parameter: the owner currently
  has no run threshold pace configured (documented gap in the ODD task),
  and a non-positive threshold raises ``ValueError`` rather than ever
  defaulting silently.

Swim pace-based load (sTSS, Coggan-style normalisation to critical swim
speed):

    NSS  = mean over valid, strictly positive speed samples (m/s)
    IF   = NSS / css
    sTSS = duration_h * IF^3 * 100

Documented semantics (see :func:`normalized_swim_speed` for details):

- Normalized swim speed is the arithmetic mean of valid, strictly
  positive speed samples. Rest intervals (zero speed), negative glitch
  values and missing samples are excluded from the mean: rest time is
  recovery, not locomotion, and its load contribution is already carried
  by the wall-clock duration term. Excluding rests keeps the intensity
  factor representative of the actual swimming while the duration term
  still accrues load for the full session.
- ``css`` (critical swim speed, m/s) is a required explicit parameter;
  a non-positive CSS raises ``ValueError`` rather than defaulting.

All functions are pure and fully typed; validation errors raise
``ValueError`` rather than clamping or silently defaulting, except the
documented clamping of ``dHRr`` to [0, 1].
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "BikePowerLoad",
    "RunPaceLoad",
    "SwimPaceLoad",
    "TrimpCoefficients",
    "bike_power_load",
    "grade_adjusted_speed",
    "hr_ratio",
    "hrtss",
    "intensity_factor",
    "minetti_energy_cost",
    "normalized_graded_speed",
    "normalized_power",
    "normalized_swim_speed",
    "power_tss",
    "run_pace_load",
    "run_pace_tss",
    "srpe_load",
    "swim_pace_load",
    "swim_tss",
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


MINETTI_CR_FLAT = 3.6
"""Minetti et al. 2002 energy cost of flat running: Cr(0) = 3.6 J kg^-1 m^-1.

Module-level constant so tests and callers can reference the normaliser of
the grade adjustment. Module-level constant owned here (LOAD-11 may move it
to settings with its source comment).
"""


def minetti_energy_cost(grade: float) -> float:
    """Energy cost of running per unit distance at grade i (J kg^-1 m^-1).

    Formula: Cr(i) = 155.4 i^5 - 30.4 i^4 - 43.3 i^3 + 46.3 i^2 + 19.5 i
    + 3.6, with i = elevation change / horizontal distance (rise over run,
    dimensionless; +0.1 = 10% uphill).

    The polynomial is fitted for grades in about [-0.20, +0.40]; outside
    that domain the value is an extrapolation and is deliberately NOT
    clamped (no silent correction of sensor data).

    Reference: Minetti, Ardigo & Capelli 2002, "Energy cost of walking and
    running at extreme uphill and downhill slopes", J Appl Physiol
    93(3):1039-1046.
    """
    return (
        155.4 * grade**5
        - 30.4 * grade**4
        - 43.3 * grade**3
        + 46.3 * grade**2
        + 19.5 * grade
        + 3.6
    )


def grade_adjusted_speed(speed_mps: float, grade: float) -> float:
    """Flat-equivalent (grade-adjusted) speed: v_flat = v * Cr(i) / Cr(0).

    Running at grade i costs Cr(i) J per metre instead of the flat cost
    Cr(0) = 3.6 (:data:`MINETTI_CR_FLAT`), so the same physical speed is
    rescaled to the speed that would cost the same energy on flat ground.
    Uphill (i > 0) yields v_flat > v; downhill (i < 0) v_flat < v.

    Reference: Minetti et al. 2002.
    """
    return speed_mps * minetti_energy_cost(grade) / MINETTI_CR_FLAT


def _sample_grade(
    index: int,
    distance_samples: Sequence[float | None] | None,
    altitude_samples: Sequence[float | None] | None,
) -> float:
    """Grade of sample k = (alt[k] - alt[k-1]) / (dist[k] - dist[k-1]).

    Returns 0.0 where the grade is unavailable: k = 0 (no preceding
    sample), a missing altitude/distance sample, a non-positive distance
    delta (GPS noise), or an absent stream. See
    :func:`normalized_graded_speed` for why 0 is the correct neutral value.
    """
    if distance_samples is None or altitude_samples is None or index == 0:
        return 0.0
    d_prev = distance_samples[index - 1]
    d_curr = distance_samples[index]
    a_prev = altitude_samples[index - 1]
    a_curr = altitude_samples[index]
    if d_prev is None or d_curr is None or a_prev is None or a_curr is None:
        return 0.0
    delta_d = d_curr - d_prev
    if delta_d <= 0.0:
        return 0.0
    return (a_curr - a_prev) / delta_d


def normalized_graded_speed(
    speed_samples: Sequence[float | None],
    *,
    distance_samples: Sequence[float | None] | None = None,
    altitude_samples: Sequence[float | None] | None = None,
) -> float:
    """Normalized graded speed (NGS): mean of per-sample grade-adjusted speeds.

    Formula: NGS = mean over valid (non-``None``) speed samples of
    ``v * Cr(grade) / Cr(0)`` (:func:`grade_adjusted_speed`), with the
    per-sample grade from :func:`_sample_grade`. An arithmetic mean is used
    (rather than Coggan's fourth-power smoothing): the pace-load curve
    already applies the quadratic IF scaling, so no additional variability
    weighting is applied — the choice is documented and deterministic.

    Grade availability: the grade of sample k is
    ``(alt[k] - alt[k-1]) / (dist[k] - dist[k-1])`` from the per-second
    altitude (m) and cumulative distance (m) streams. Where it is
    unavailable — k = 0, missing (``None``) samples, a non-positive
    distance delta (GPS noise), or an altitude/distance stream that is
    ``None`` entirely — the grade is treated as 0. ``Cr(0) = 3.6`` makes
    grade 0 the neutral element (adjusted speed = measured speed), so this
    is a documented absence semantics, never a claim that the terrain was
    flat.

    Missing speed samples (``None``) are excluded from the mean, mirroring
    the power-stream gap semantics of :func:`normalized_power`. Streams of
    different lengths raise ``ValueError`` (the ingest parser emits
    aligned streams; misalignment is a bug, not a graceful-degradation
    case). Zero valid samples raise ``ValueError`` ("no usable run speed
    data").

    Reference: Minetti et al. 2002; NGS/IF/rTSS convention per Coggan-style
    pace normalisation (1 h at threshold run speed = 100 rTSS).
    """
    if distance_samples is not None and len(distance_samples) != len(speed_samples):
        raise ValueError(
            "speed and distance streams must have the same length: "
            f"{len(speed_samples)} != {len(distance_samples)}"
        )
    if altitude_samples is not None and len(altitude_samples) != len(speed_samples):
        raise ValueError(
            "speed and altitude streams must have the same length: "
            f"{len(speed_samples)} != {len(altitude_samples)}"
        )
    graded: list[float] = []
    for index, speed in enumerate(speed_samples):
        if speed is None:
            continue  # stream gap: excluded from the mean
        grade = _sample_grade(index, distance_samples, altitude_samples)
        graded.append(grade_adjusted_speed(speed, grade))
    if not graded:
        raise ValueError(
            "no usable run speed data: samples sequence is empty or all missing"
        )
    return math.fsum(graded) / len(graded)


def run_pace_tss(
    duration_s: float, ngs_mps: float, threshold_run_speed_mps: float
) -> float:
    """Run Training Stress Score: rTSS = duration_h * IF^2 * 100.

    Formula: IF = NGS / threshold_run_speed; rTSS = (duration_s / 3600) *
    IF^2 * 100, so one hour at NGS = threshold run speed scores exactly
    100 rTSS.

    ``threshold_run_speed`` is required and non-positive values raise
    ``ValueError``: the owner has no run threshold pace configured yet
    (documented gap), and a missing threshold must never silently default.
    Non-positive duration or a negative NGS also raise ``ValueError``.

    Reference: Coggan-style pace normalisation; grade model per Minetti
    et al. 2002.
    """
    if duration_s <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_s!r} seconds")
    if threshold_run_speed_mps <= 0.0:
        raise ValueError(
            "threshold_run_speed must be positive, got "
            f"{threshold_run_speed_mps!r} m/s (owner has no run threshold "
            "pace configured; supply an explicit positive value)"
        )
    if ngs_mps < 0.0:
        raise ValueError(f"NGS must be non-negative, got {ngs_mps!r} m/s")
    if_value = ngs_mps / threshold_run_speed_mps
    return duration_s / 3600.0 * math.pow(if_value, 2) * 100.0


@dataclass(frozen=True, slots=True)
class RunPaceLoad:
    """Run pace-based load results consumed by method selection (LOAD-6).

    ``normalized_graded_speed`` in m/s, ``intensity_factor`` dimensionless,
    ``tss`` on the rTSS scale (1 h at threshold run speed = 100).
    """

    normalized_graded_speed: float
    intensity_factor: float
    tss: float


def run_pace_load(
    speed_samples: Sequence[float | None],
    *,
    duration_s: float,
    threshold_run_speed: float,
    distance_samples: Sequence[float | None] | None = None,
    altitude_samples: Sequence[float | None] | None = None,
) -> RunPaceLoad:
    """Convenience entry point: NGS, IF and rTSS for one run session.

    Combines :func:`normalized_graded_speed` (grade-unavailability and
    missing-sample semantics documented there) and :func:`run_pace_tss`.
    ``duration_s`` is the session wall-clock duration in seconds (explicit,
    because a gapped per-second stream has fewer valid samples than
    seconds elapsed). Returns a :class:`RunPaceLoad` for downstream method
    selection.

    Reference: Minetti et al. 2002; Coggan-style pace normalisation.
    """
    ngs = normalized_graded_speed(
        speed_samples,
        distance_samples=distance_samples,
        altitude_samples=altitude_samples,
    )
    # run_pace_tss validates the threshold (ValueError) before any division.
    tss = run_pace_tss(duration_s, ngs, threshold_run_speed)
    return RunPaceLoad(
        normalized_graded_speed=ngs,
        intensity_factor=ngs / threshold_run_speed,
        tss=tss,
    )


def normalized_swim_speed(speed_samples: Sequence[float | None]) -> float:
    """Normalized swim speed (NSS): mean of valid, strictly positive speeds.

    Formula: NSS = mean over valid, strictly positive speed samples (m/s).

    Rest (zero-speed), negative glitch values and missing (``None``)
    samples are excluded from the mean. Rationale: rest time is recovery,
    not locomotion, and its load contribution is already carried by the
    wall-clock ``duration_s`` term of :func:`swim_tss`; excluding rests
    keeps the intensity factor representative of the actual swimming
    instead of deflating it with stoppage time. A series with no strictly
    positive sample raises ``ValueError`` ("no usable swim speed data") —
    an all-rest session is not silently scored.

    Reference: Coggan-style pace normalisation to critical swim speed
    (1 h at CSS = 100 sTSS).
    """
    valid = [s for s in speed_samples if s is not None and s > 0.0]
    if not valid:
        raise ValueError(
            "no usable swim speed data: samples sequence is empty, all "
            "missing, or contains no strictly positive (swimming) sample"
        )
    return math.fsum(valid) / len(valid)


def swim_tss(duration_s: float, nss_mps: float, css_mps: float) -> float:
    """Swim Training Stress Score: sTSS = duration_h * IF^3 * 100.

    Formula: IF = NSS / CSS; sTSS = (duration_s / 3600) * IF^3 * 100, so
    one hour at NSS = CSS scores exactly 100 sTSS. The cubic intensity
    exponent reflects the extreme sensitivity of swim cost to velocity.

    Non-positive CSS (misconfigured threshold), non-positive duration, or
    a negative NSS raise ``ValueError``.

    Reference: Coggan-style pace normalisation to critical swim speed.
    """
    if duration_s <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_s!r} seconds")
    if css_mps <= 0.0:
        raise ValueError(f"CSS must be positive, got {css_mps!r} m/s")
    if nss_mps < 0.0:
        raise ValueError(f"NSS must be non-negative, got {nss_mps!r} m/s")
    if_value = nss_mps / css_mps
    return duration_s / 3600.0 * math.pow(if_value, 3) * 100.0


@dataclass(frozen=True, slots=True)
class SwimPaceLoad:
    """Swim pace-based load results consumed by method selection (LOAD-6).

    ``normalized_swim_speed`` in m/s, ``intensity_factor`` dimensionless,
    ``tss`` on the sTSS scale (1 h at CSS = 100).
    """

    normalized_swim_speed: float
    intensity_factor: float
    tss: float


def swim_pace_load(
    speed_samples: Sequence[float | None],
    *,
    duration_s: float,
    css: float,
) -> SwimPaceLoad:
    """Convenience entry point: NSS, IF and sTSS for one swim session.

    Combines :func:`normalized_swim_speed` (rest/missing-sample semantics
    documented there) and :func:`swim_tss`. ``duration_s`` is the session
    wall-clock duration in seconds (explicit, because rest intervals make
    the valid-sample count smaller than the elapsed seconds). Returns a
    :class:`SwimPaceLoad` for downstream method selection.

    Reference: Coggan-style pace normalisation to critical swim speed.
    """
    nss = normalized_swim_speed(speed_samples)
    # swim_tss validates the CSS (ValueError) before any division.
    tss = swim_tss(duration_s, nss, css)
    return SwimPaceLoad(
        normalized_swim_speed=nss,
        intensity_factor=nss / css,
        tss=tss,
    )
