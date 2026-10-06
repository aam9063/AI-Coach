"""Readiness signals: HRV, resting heart rate and sleep against the
athlete's OWN baseline (PROJECT_BRIEF section 7.4).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core`` (PROJECT_BRIEF sections 6 and 14). Daily measurements come in
as an explicit ``Mapping[date, float | None]`` — one entry per calendar day
(rest days and unmeasured days as ``None``); windows, the SD band and every
threshold are parameters. Side effects, persistence and settings reads
belong in callers. In particular this module deliberately does NOT import
``app.core.settings``: the settings wiring with source comments is RID-10's
job (Feature 5), mirrored later like the LOAD-11/ZON-11 patterns.

Formulas and references
-----------------------
HRV (rid RID-1/RID-2, section 7.4):

    observed = mean of the trailing 7 daily ln(rMSSD) values
    baseline = mean and SAMPLE SD (ddof=1) of the previous 60 daily
               ln(rMSSD) values, excluding the 7-day window
    flagged  = observed strictly outside baseline_mean ± 0.5 x baseline_SD

The natural-log transform of rMSSD stabilises the day-to-day variance and
is the standard preprocessing for HRV trend analysis (Plews et al. 2013,
"Training adaptation and heart rate variability in elite endurance
athletes"; Buchheit 2014, "Monitoring training status with HRV measures").
The 0.5 SD half band is the smallest-worthwhile-change (SWC) approach:
changes smaller than half the athlete's own typical variation are treated
as noise (Kiviniemi et al. 2007; Plews et al. 2013; section 7.4 fixes the
7-day / 60-day / ± 0.5 SD triple). The flag fires on BOTH sides of the
band: a suppressed rolling mean ("low") is the classic overreaching signal
(Plews et al. 2013) and an unusually elevated one ("elevated") can mark
recovery above baseline.

Resting heart rate (section 7.4): the observed value is the daily resting
HR on the as-of date; the baseline is the athlete's own previous 30 days
(mean and sample SD, ddof=1). The same ± 0.5 SD SWC band is applied — an
OWNER CHOICE extending the HRV smallest-worthwhile-change logic to resting
HR, kept as the explicit parameter ``band_sd``.

Sleep (section 7.4): duration vs the athlete's own baseline, identical
treatment to resting HR. The brief fixes no window for sleep, so the 30-day
baseline default is an OWNER CHOICE kept symmetric with resting HR.

Window semantics (documented precisely, asserted by the tests)
--------------------------------------------------------------
- Every evaluation is anchored on the LAST date of the passed mapping (the
  as-of date); to evaluate an earlier day, pass a truncated mapping.
- HRV: the rolling window is the trailing ``window_days`` (default 7)
  calendar days ENDING INCLUSIVE on the as-of date. The baseline is the
  ``baseline_days`` (default 60) calendar days IMMEDIATELY PRECEDING the
  rolling window — the two windows NEVER overlap (the baseline ends the day
  before the window starts). The tests pin this with a monotone-drift
  probe: excluding the window keeps a sustained 7-day shift fully outside
  the baseline instead of diluting it into it.
- Resting HR / sleep: the baseline is the ``baseline_days`` (default 30)
  calendar days IMMEDIATELY PRECEDING the as-of date, EXCLUSIVE of it — the
  observed day never contributes to its own baseline.
- Missing measurements are explicit ``None`` values (never omitted keys: a
  gap between consecutive dates raises ``ValueError``, the same convention
  as ``app.engine.pmc`` for daily series). Mean and SD use only the valid
  (non-``None``) days.

Band semantics
--------------
- "Outside the band" is STRICT: a rolling mean exactly AT
  baseline_mean ± 0.5 SD does NOT flag, mirroring the ZON-9 margin
  precedent (189.00 W at exactly +5% does not propose). The boundary is
  compared tolerantly (``_same_boundary``, the same rule as the HR zone
  bounds), so float noise at the boundary cannot flip the outcome.
- The baseline SD is the SAMPLE SD (ddof=1), which needs at least two valid
  baseline days; ``min_baseline_valid_days < 2`` raises ``ValueError``.
- Constant baseline (SD = 0), documented OWNER CHOICE: the ± 0.5 SD band
  collapses to the baseline mean itself, and any rolling mean tolerantly
  different from the mean is outside the band (mathematically |deviation| /
  0 is beyond any finite band). The deviation-in-SD field is ``None`` in
  that case (undefined, never ``inf``) and the ``detail`` text says so.

Multi-signal warning rule (RID-3) and structured assessment (RID-4)
--------------------------------------------------------------------
:class:`ReadinessAssessment` / :func:`readiness_assessment` combine the
three readiness signals with the two context signals the brief names — TSB
from the load model (section 7.4) and an optional subjective-fatigue
report — into the multi-signal warning rule. The rule is EXACTLY "two or
more adverse signals agree" (section 7.4, section 12.5 acceptance): a
single adverse signal alone must never produce a reduce-intensity
suggestion. Adverse semantics: HRV direction ``"low"`` (suppressed rolling
mean, the classic overreaching signal — Plews et al. 2013), resting HR
``"elevated"``, sleep ``"low"`` (short), TSB below the configurable
"very negative" threshold (``tsb_very_negative_below``, default -10.0,
OWNER CHOICE — provenance in "Configurable constants") and a reported
subjective fatigue. A signal with ``status == "insufficient_data"`` NEVER
counts as agreeing: absence of evidence is not evidence. ACWR is accepted
as context-only input (section 7.2, Impellizzeri et al. 2020) and is
carried through the result verbatim without ever entering the verdict.
The suggestion is a "consider reducing intensity" statement — never a
diagnosis or medical advice (section 3). When every warning signal is
adverse the suggestion additionally proposes rest and recovery, still as
a suggestion (documented OWNER CHOICE).

:class:`ReadinessAssessment` (RID-4) is the structured output object:
every signal with its direction and confidence, the adverse-signal keys,
the agreement count, the explicit ``suggest_reduce_intensity`` boolean,
the human-readable reasons and the context-only inputs. It carries NO
composite score and NO single readiness number (section 7.4). It is what
the future ``get_readiness`` tool wraps (Feature 6, section 9.3). The
shape is "typed Pydantic-ready" without importing pydantic into the
engine: every field is a primitive or a tuple of frozen dataclasses, so
``dataclasses.asdict`` yields a nested primitive structure that maps 1:1
onto nested ``pydantic.BaseModel`` classes with identical field names
(tuples become lists); that mapping belongs to the future tool layer
(e.g. ``app/services/``), never to ``app/engine/``.

Structured output (never a single score)
----------------------------------------
Each signal returns a frozen :class:`ReadinessSignal` carrying its stable
key, the observed value, the baseline (mean and SD), the deviation in
signal units and in SD, the direction, the confidence and a human-readable
``detail``. There is deliberately NO composite readiness score anywhere in
this module: section 7.4 requires a structured multi-signal object, and the
multi-signal warning rule ("two or more signals agree", RID-3) lives in its
own layer — a single flagged signal must never suggest reduced intensity by
itself.

Direction vocabulary (``ReadinessDirection``): ``"elevated"`` / ``"normal"``
/ ``"low"`` describe the OBSERVED VALUE's relation to the baseline band —
never the advice. Interpretation (e.g. elevated resting HR = concern, but
elevated HRV after a taper = good) belongs to the warning-rule layer. When
the signal cannot be assessed the direction is ``"insufficient_data"``.

Insufficient data
-----------------
Where a measurement is absent the engine reports it as missing rather than
substituting a default (section 12 / data note: the owner's wellness rows
are only partly populated):

- a ``None`` inside the BASELINE reduces the evidence and the confidence
  but the signal still assesses;
- a ``None`` inside the HRV rolling window (or too few valid days under
  ``min_window_valid_fraction``), a missing observation on the as-of date
  (resting HR / sleep), or fewer valid baseline days than
  ``min_baseline_valid_days`` returns an explicit
  ``status="insufficient_data"`` result: ``direction="insufficient_data"``,
  ``flagged=None``, numeric observation fields ``None`` — never a fabricated
  value and never an exception.

Confidence (documented derivation)
----------------------------------
``confidence = min(1.0, n_window_valid / window_days,
n_baseline_valid / baseline_days)`` — the coverage fraction of the WEAKER of
the two evidence sources, capped at 1.0. A fully populated 7-day window and
60-day baseline gives 1.0; one missing baseline day gives 59/60 ≈ 0.983;
for resting HR / sleep the "window" is the single as-of observation, so the
confidence is the baseline coverage fraction. An ``insufficient_data``
result carries ``confidence = 0.0``. The counts behind it
(``n_window_valid`` / ``n_baseline_valid``) are always reported so the
confidence is auditable.

Configurable constants (RID-2, §7/§14)
--------------------------------------
Every constant below is a function parameter whose default is the
documented module constant (the documented fallback). This module never
imports settings (§6 purity); mirroring into ``app.core.settings`` with
source comments is RID-10's scope:

- HRV rolling window 7 days (``window_days``): LITERATURE (section 7.4;
  Plews et al. 2013 rolling-average convention, Kiviniemi et al. 2007).
- HRV baseline 60 days (``baseline_days``): LITERATURE (section 7.4;
  Plews et al. 2013).
- HRV band ± 0.5 SD (``band_sd``): LITERATURE (smallest worthwhile change;
  Kiviniemi et al. 2007, section 7.4).
- Resting-HR baseline 30 days (``baseline_days``): LITERATURE (section 7.4).
- Resting-HR band ± 0.5 SD (``band_sd``): OWNER CHOICE — the section 7.4
  SWC logic extended to resting HR (Plews et al. 2013 also track resting
  HR against the athlete's own normal range).
- Sleep baseline 30 days (``baseline_days``): OWNER CHOICE — the brief
  fixes no sleep window; kept symmetric with resting HR.
- Sleep band ± 0.5 SD (``band_sd``): OWNER CHOICE — same SWC extension.
- TSB "very negative" threshold -10.0 (``tsb_very_negative_below``):
  OWNER CHOICE — the brief says "very negative" without a number (the
  warning-rule example is "TSB very negative", section 7.4). TSB =
  CTL_{t-1} - ATL_{t-1} is expressed in TSS/day; the TrainingPeaks / Friel
  coaching convention treats form below about -10 as the onset of the
  high-fatigue zone and below -30 as deep-overreach territory. -10.0 is
  chosen deliberately conservatively (fires EARLIER than -30) because the
  rule already requires two or more agreeing signals before it suggests
  anything, and a "consider reducing intensity" nudge must arrive well
  before forced rest becomes the topic.
- Baseline completeness 42 of 60 days (HRV) and 21 of 30 days (resting HR /
  sleep), i.e. 70% (``min_baseline_valid_days``): OWNER CHOICE — the owner's
  wellness rows are partly populated, so a strict 100% requirement would
  leave the signals permanently unassessable; 70% keeps the statistics
  meaningful while tolerating real gaps.
- Rolling-window completeness (``min_window_valid_fraction``, default 1.0 —
  only fully complete windows assess): OWNER CHOICE, mirroring the strict
  gap-rule default of ``app.engine.zones`` / ``app.engine.load``; the
  flagged quantity must rest on a complete window, relax explicitly for
  gap-heavy data.
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final, Literal

__all__ = [
    "DEFAULT_HRV_BAND_SD",
    "DEFAULT_HRV_BASELINE_DAYS",
    "DEFAULT_HRV_MIN_BASELINE_VALID_DAYS",
    "DEFAULT_HRV_WINDOW_DAYS",
    "DEFAULT_MIN_WINDOW_VALID_FRACTION",
    "DEFAULT_RHR_BAND_SD",
    "DEFAULT_RHR_BASELINE_DAYS",
    "DEFAULT_RHR_MIN_BASELINE_VALID_DAYS",
    "DEFAULT_SLEEP_BAND_SD",
    "DEFAULT_SLEEP_BASELINE_DAYS",
    "DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS",
    "DEFAULT_TSB_VERY_NEGATIVE",
    "READINESS_DIRECTIONS",
    "SIGNAL_KEYS",
    "WARNING_SIGNAL_KEYS",
    "ReadinessAssessment",
    "ReadinessDirection",
    "ReadinessSignal",
    "SignalKey",
    "SignalStatus",
    "WarningSignalKey",
    "hrv_readiness",
    "readiness_assessment",
    "resting_hr_readiness",
    "sleep_readiness",
]


# ---------------------------------------------------------------------------
# Configurable constants (RID-2, §7/§14) — provenance in the module docstring
# ---------------------------------------------------------------------------

DEFAULT_HRV_WINDOW_DAYS: Final[int] = 7
"""HRV rolling window in days (section 7.4; Plews et al. 2013). LITERATURE."""

DEFAULT_HRV_BASELINE_DAYS: Final[int] = 60
"""HRV baseline window in days (section 7.4; Plews et al. 2013). LITERATURE."""

DEFAULT_HRV_BAND_SD: Final[float] = 0.5
"""HRV band half width in baseline SDs: smallest worthwhile change
(Kiviniemi et al. 2007; section 7.4). LITERATURE."""

DEFAULT_HRV_MIN_BASELINE_VALID_DAYS: Final[int] = 42
"""Minimum valid days inside the 60-day HRV baseline (70%): OWNER CHOICE."""

DEFAULT_RHR_BASELINE_DAYS: Final[int] = 30
"""Resting-HR baseline window in days (section 7.4). LITERATURE."""

DEFAULT_RHR_BAND_SD: Final[float] = 0.5
"""Resting-HR band half width in baseline SDs: OWNER CHOICE (SWC extension)."""

DEFAULT_RHR_MIN_BASELINE_VALID_DAYS: Final[int] = 21
"""Minimum valid days inside the 30-day resting-HR baseline (70%): OWNER
CHOICE."""

DEFAULT_SLEEP_BASELINE_DAYS: Final[int] = 30
"""Sleep baseline window in days: OWNER CHOICE (symmetric with resting HR)."""

DEFAULT_SLEEP_BAND_SD: Final[float] = 0.5
"""Sleep band half width in baseline SDs: OWNER CHOICE (SWC extension)."""

DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS: Final[int] = 21
"""Minimum valid days inside the 30-day sleep baseline (70%): OWNER CHOICE."""

DEFAULT_MIN_WINDOW_VALID_FRACTION: Final[float] = 1.0
"""Fraction of the HRV rolling window that must carry a measurement
(default strict, mirroring ``app.engine.zones``): OWNER CHOICE."""

DEFAULT_TSB_VERY_NEGATIVE: Final[float] = -10.0
"""TSB strictly below this value counts as "very negative" (adverse):
OWNER CHOICE — the brief says "very negative" without a number (section
7.4). TSB = CTL_{t-1} - ATL_{t-1} in TSS/day; the TrainingPeaks / Friel
convention places the onset of the high-fatigue zone around -10 and deep
overreach below -30. -10.0 fires EARLIER on purpose: the rule already
requires two or more agreeing signals, and a "consider reducing
intensity" nudge must arrive before forced rest is the topic."""


# ---------------------------------------------------------------------------
# Stable keys and vocabularies
# ---------------------------------------------------------------------------

SignalKey = Literal["hrv_ln_rmssd", "resting_hr", "sleep_duration"]
"""Stable machine-readable keys of the three readiness signals (section 7.4)."""

SIGNAL_KEY_HRV: Final[SignalKey] = "hrv_ln_rmssd"
"""Daily ln(rMSSD) signal."""

SIGNAL_KEY_RHR: Final[SignalKey] = "resting_hr"
"""Daily resting heart-rate signal (bpm)."""

SIGNAL_KEY_SLEEP: Final[SignalKey] = "sleep_duration"
"""Daily sleep-duration signal (any consistent unit, e.g. hours)."""

SIGNAL_KEYS: Final[tuple[SignalKey, ...]] = (
    SIGNAL_KEY_HRV,
    SIGNAL_KEY_RHR,
    SIGNAL_KEY_SLEEP,
)
"""All readiness signal keys (the stable, validated signal namespace)."""

SignalStatus = Literal["assessed", "insufficient_data"]
"""Assessment status: ``"assessed"`` when the evidence was sufficient,
``"insufficient_data"`` when it was not (reported explicitly, never
substituted)."""

ReadinessDirection = Literal["elevated", "normal", "low", "insufficient_data"]
"""Direction of the observed value relative to the baseline band.

``"elevated"`` / ``"low"``: strictly beyond the band on the respective side.
``"normal"``: inside the band (including exactly at the strict boundary).
``"insufficient_data"``: the signal could not be assessed. The direction
describes the VALUE, never the advice (see the module docstring)."""

READINESS_DIRECTIONS: Final[tuple[ReadinessDirection, ...]] = (
    "elevated",
    "normal",
    "low",
    "insufficient_data",
)
"""All readiness direction keys (stable vocabulary for callers)."""

WarningSignalKey = Literal[
    "hrv_ln_rmssd",
    "resting_hr",
    "sleep_duration",
    "tsb_very_negative",
    "subjective_fatigue",
]
"""Stable keys of the five warning-rule inputs (section 7.4): the three
readiness signals plus TSB (as a "very negative" flag) and the subjective
fatigue report."""

WARNING_SIGNAL_KEYS: Final[tuple[WarningSignalKey, ...]] = (
    "hrv_ln_rmssd",
    "resting_hr",
    "sleep_duration",
    "tsb_very_negative",
    "subjective_fatigue",
)
"""All warning-signal keys, in the stable evaluation order of
:func:`readiness_assessment`."""

ADVERSE_DIRECTIONS: Final[dict[SignalKey, ReadinessDirection]] = {
    SIGNAL_KEY_HRV: "low",
    SIGNAL_KEY_RHR: "elevated",
    SIGNAL_KEY_SLEEP: "low",
}
"""The observed-value direction that counts as ADVERSE per signal.

The direction vocabulary describes the VALUE (module docstring), so the
warning rule needs this explicit interpretation layer: a SUPPRESSED HRV
rolling mean (``"low"``) is the classic overreaching signal (Plews et al.
2013) while an ELEVATED one can mark recovery above baseline (not
adverse); an ELEVATED resting HR is the concern (section 7.4) while a low
one typically reflects fitness; SHORT sleep (``"low"``) is the concern
while unusually long sleep is not a warning."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadinessSignal:
    """One structured readiness signal (section 7.4), never a score.

    Frozen and slotted, consistent with the other engine result
    conventions. There is deliberately no composite/readiness-score field:
    section 7.4 requires a structured multi-signal object and the
    multi-signal warning rule (RID-3) lives outside this module.

    ``observed`` is the rolling-mean (HRV) or as-of-day (resting HR / sleep)
    value; ``baseline_mean`` / ``baseline_sd`` describe the athlete's own
    baseline (sample SD, ddof=1, over the valid baseline days only);
    ``deviation`` = observed - baseline_mean in signal units and
    ``deviation_in_sd`` = deviation / baseline_sd (``None`` when the SD is
    0 — undefined, never ``inf``). ``flagged`` is ``True`` only when the
    observed value is strictly beyond baseline ± ``band_half_width_sd`` SD;
    it is ``None`` for ``insufficient_data`` results. ``confidence`` and the
    ``n_*_valid`` counts follow the documented derivation (module
    docstring, "Confidence"). ``detail`` is a human-readable audit line.
    """

    key: SignalKey
    status: SignalStatus
    direction: ReadinessDirection
    confidence: float
    observed: float | None
    baseline_mean: float | None
    baseline_sd: float | None
    deviation: float | None
    deviation_in_sd: float | None
    flagged: bool | None
    band_half_width_sd: float
    window_days: int
    baseline_days: int
    n_window_valid: int
    n_baseline_valid: int
    detail: str


# ---------------------------------------------------------------------------
# Multi-signal warning rule (RID-3) and structured assessment (RID-4)
# ---------------------------------------------------------------------------

SUGGESTION_REDUCE_INTENSITY: Final[str] = "Consider reducing intensity today."
"""The neutral suggestion when two or more adverse signals agree: a
suggestion, never a diagnosis or medical advice (section 3)."""

SUGGESTION_REDUCE_AND_REST: Final[str] = (
    "Consider reducing intensity today and prioritize rest and recovery."
)
"""The suggestion when EVERY warning signal is adverse (OWNER CHOICE to
expose a rest/recovery phrasing; still a suggestion, never a diagnosis)."""

_SIGNAL_LABELS: Final[dict[SignalKey, str]] = {
    SIGNAL_KEY_HRV: "HRV (ln rMSSD)",
    SIGNAL_KEY_RHR: "Resting heart rate",
    SIGNAL_KEY_SLEEP: "Sleep duration",
}
"""Human-readable labels used in the assessment reasons."""


@dataclass(frozen=True, slots=True)
class ReadinessAssessment:
    """The structured multi-signal readiness assessment (RID-4, section 7.4).

    This is the object the future ``get_readiness`` tool wraps (Feature 6,
    section 9.3): every signal with its direction and confidence (the
    frozen :class:`ReadinessSignal` tuple), the adverse-signal keys, the
    agreement count, the explicit ``suggest_reduce_intensity`` boolean,
    the human-readable ``reasons`` (one per adverse warning signal, in the
    stable :data:`WARNING_SIGNAL_KEYS` order) and the context-only inputs
    (``tsb``, ``tsb_very_negative_below``, ``subjective_fatigue_reported``
    and ``acwr`` — ACWR is context ONLY, never part of the verdict,
    section 7.2 / Impellizzeri et al. 2020).

    There is deliberately NO composite score and NO single readiness
    number: section 7.4 requires a structured multi-signal object. The
    shape is typed Pydantic-ready without importing pydantic here: every
    field is a primitive or a tuple of frozen dataclasses, so
    ``dataclasses.asdict`` yields a nested primitive structure that maps
    1:1 onto nested ``pydantic.BaseModel`` classes with identical field
    names (tuples become lists). That mapping belongs to the future tool
    layer (e.g. ``app/services/``), never to ``app/engine/``.
    """

    signals: tuple[ReadinessSignal, ...]
    adverse_signal_keys: tuple[WarningSignalKey, ...]
    agreement_count: int
    suggest_reduce_intensity: bool
    suggestion: str | None
    reasons: tuple[str, ...]
    tsb: float
    tsb_very_negative_below: float
    subjective_fatigue_reported: bool | None
    acwr: float | None


# ---------------------------------------------------------------------------
# Shared internals
# ---------------------------------------------------------------------------

_BOUNDARY_REL_TOL: Final = 1e-12
_BOUNDARY_ABS_TOL: Final = 1e-12


def _same_boundary(a: float, b: float) -> bool:
    """Whether two band-boundary quantities are the same within float noise.

    The same tolerant-boundary rule as ``app.engine.zones._same_boundary``:
    a deviation-in-SD computed as ``0.5000000000000001`` at a true 0.5
    boundary must not flip the strict "outside the band" outcome. The
    tolerances (1e-12 relative, 1e-12 absolute) are many orders of
    magnitude tighter than any real physiological deviation.
    """
    return math.isclose(a, b, rel_tol=_BOUNDARY_REL_TOL, abs_tol=_BOUNDARY_ABS_TOL)


def _validated_daily_series(
    series: Mapping[dt.date, float | None],
    *,
    quantity: str,
    positive: bool = False,
) -> list[tuple[dt.date, float | None]]:
    """Validate a daily measurement mapping and return it date-ordered.

    An empty mapping, a gap between consecutive dates (missing days must be
    present as explicit ``None`` — the ``app.engine.pmc`` daily-series
    convention), a non-finite value and (when ``positive``) a non-positive
    value all raise ``ValueError`` instead of degrading silently.
    """
    if not series:
        raise ValueError(f"no {quantity} data: the daily series is empty")
    entries = [(day, series[day]) for day in sorted(series)]
    for (day_a, _), (day_b, _) in pairwise(entries):
        if (day_b - day_a).days != 1:
            raise ValueError(
                f"{quantity} series has a calendar gap between "
                f"{day_a.isoformat()} and {day_b.isoformat()}: missing days "
                "must be present explicitly as None, never omitted"
            )
    for day, value in entries:
        if value is None:
            continue
        if not math.isfinite(value):
            raise ValueError(
                f"{quantity} value on {day.isoformat()} is not finite: {value!r}"
            )
        if positive and value <= 0.0:
            raise ValueError(
                f"{quantity} value on {day.isoformat()} must be positive, "
                f"got {value!r}"
            )
    return entries


def _baseline_stats(valid_values: Sequence[float]) -> tuple[float, float]:
    """Baseline mean and SAMPLE SD (ddof=1) over the valid days only.

    Sums go through :func:`math.fsum` (engine convention). The caller
    guarantees at least two values (ddof=1 needs two).
    """
    n = len(valid_values)
    mean = math.fsum(valid_values) / n
    ss = math.fsum((value - mean) ** 2 for value in valid_values)
    return mean, math.sqrt(ss / (n - 1))


def _band_decision(
    *,
    observed: float,
    baseline_mean: float,
    baseline_sd: float,
    band_sd: float,
) -> tuple[bool, float, float | None, ReadinessDirection]:
    """Strict band comparison with the tolerant boundary rule.

    Returns ``(flagged, deviation, deviation_in_sd, direction)``. Exactly at
    the boundary does NOT flag (ZON-9 margin precedent); a constant baseline
    (SD = 0) collapses the band to the baseline mean itself (documented
    OWNER CHOICE, module docstring "Band semantics") and reports
    ``deviation_in_sd = None``.
    """
    deviation = observed - baseline_mean
    if baseline_sd > 0.0:
        deviation_in_sd = deviation / baseline_sd
        magnitude = abs(deviation_in_sd)
        flagged = magnitude > band_sd and not _same_boundary(magnitude, band_sd)
    else:
        deviation_in_sd = None
        flagged = not _same_boundary(deviation, 0.0)
    if not flagged:
        direction: ReadinessDirection = "normal"
    else:
        direction = "elevated" if deviation > 0.0 else "low"
    return flagged, deviation, deviation_in_sd, direction


def _validate_baseline_params(
    *,
    baseline_days: int,
    band_sd: float,
    min_baseline_valid_days: int,
) -> None:
    if baseline_days < 1:
        raise ValueError(f"baseline_days must be at least 1, got {baseline_days!r}")
    if band_sd < 0.0:
        raise ValueError(f"band_sd must not be negative, got {band_sd!r}")
    if min_baseline_valid_days < 2:
        raise ValueError(
            "min_baseline_valid_days must be at least 2: the sample SD "
            f"(ddof=1) needs two valid baseline days, got {min_baseline_valid_days!r}"
        )
    if min_baseline_valid_days > baseline_days:
        raise ValueError(
            f"min_baseline_valid_days ({min_baseline_valid_days!r}) cannot "
            f"exceed baseline_days ({baseline_days!r})"
        )


def _insufficient_signal(
    key: SignalKey,
    *,
    detail: str,
    band_sd: float,
    window_days: int,
    baseline_days: int,
    n_window_valid: int,
    n_baseline_valid: int,
) -> ReadinessSignal:
    """The explicit ``insufficient_data`` result (never a default value)."""
    return ReadinessSignal(
        key=key,
        status="insufficient_data",
        direction="insufficient_data",
        confidence=0.0,
        observed=None,
        baseline_mean=None,
        baseline_sd=None,
        deviation=None,
        deviation_in_sd=None,
        flagged=None,
        band_half_width_sd=band_sd,
        window_days=window_days,
        baseline_days=baseline_days,
        n_window_valid=n_window_valid,
        n_baseline_valid=n_baseline_valid,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# HRV signal (RID-1 tests / RID-2 implementation)
# ---------------------------------------------------------------------------


def hrv_readiness(
    series: Mapping[dt.date, float | None],
    *,
    window_days: int = DEFAULT_HRV_WINDOW_DAYS,
    baseline_days: int = DEFAULT_HRV_BASELINE_DAYS,
    band_sd: float = DEFAULT_HRV_BAND_SD,
    min_window_valid_fraction: float = DEFAULT_MIN_WINDOW_VALID_FRACTION,
    min_baseline_valid_days: int = DEFAULT_HRV_MIN_BASELINE_VALID_DAYS,
) -> ReadinessSignal:
    """HRV readiness: 7-day rolling ln(rMSSD) mean vs the 60-day baseline.

    Formula (section 7.4; Plews et al. 2013, Kiviniemi et al. 2007):

        observed = mean of the trailing ``window_days`` (default 7) daily
                   ln(rMSSD) values, ending INCLUSIVE on the as-of date
                   (the mapping's last date)
        baseline = mean and SAMPLE SD (ddof=1) of the ``baseline_days``
                   (default 60) daily values immediately PRECEDING the
                   rolling window (the windows never overlap)
        flagged  = observed strictly outside baseline ± ``band_sd``
                   (default 0.5) x baseline SD — exactly at the boundary
                   does not flag

    Window, band and completeness parameters are documented in the module
    docstring ("Window semantics", "Band semantics", "Configurable
    constants"). ``None`` marks a missing measurement: inside the baseline
    it reduces the confidence, inside the rolling window (or when the
    baseline holds fewer than ``min_baseline_valid_days`` valid days) the
    signal is reported as explicit ``insufficient_data`` — never substituted.

    Returns a frozen :class:`ReadinessSignal` with key
    ``"hrv_ln_rmssd"``; see the module docstring for the confidence
    derivation. Raises ``ValueError`` on an empty series, a calendar gap,
    a non-finite value or invalid parameters.
    """
    if window_days < 1:
        raise ValueError(f"window_days must be at least 1, got {window_days!r}")
    if not 0.0 < min_window_valid_fraction <= 1.0:
        raise ValueError(
            "min_window_valid_fraction must be within (0, 1], got "
            f"{min_window_valid_fraction!r}"
        )
    _validate_baseline_params(
        baseline_days=baseline_days,
        band_sd=band_sd,
        min_baseline_valid_days=min_baseline_valid_days,
    )
    entries = _validated_daily_series(series, quantity="ln(rMSSD)")

    # Baseline valid count is computed even when the window is incomplete:
    # the insufficient result reports whatever evidence exists.
    baseline_entries = entries[-(window_days + baseline_days) : -window_days]
    baseline_values = [value for _, value in baseline_entries if value is not None]
    n_baseline_valid = len(baseline_values)

    window_entries = entries[-window_days:]
    window_values = [value for _, value in window_entries if value is not None]
    n_window_valid = len(window_values)
    required_window_valid = math.ceil(min_window_valid_fraction * window_days)
    if n_window_valid < required_window_valid:
        return _insufficient_signal(
            SIGNAL_KEY_HRV,
            detail=(
                f"HRV rolling window incomplete: requires {required_window_valid} "
                f"of {window_days} days with a measurement, found {n_window_valid}; "
                "a missing day is reported, never substituted"
            ),
            band_sd=band_sd,
            window_days=window_days,
            baseline_days=baseline_days,
            n_window_valid=n_window_valid,
            n_baseline_valid=n_baseline_valid,
        )

    if n_baseline_valid < min_baseline_valid_days:
        return _insufficient_signal(
            SIGNAL_KEY_HRV,
            detail=(
                f"HRV baseline history short: requires {min_baseline_valid_days} "
                f"of {baseline_days} baseline days with a measurement, found "
                f"{n_baseline_valid}; a missing day is reported, never substituted"
            ),
            band_sd=band_sd,
            window_days=window_days,
            baseline_days=baseline_days,
            n_window_valid=n_window_valid,
            n_baseline_valid=n_baseline_valid,
        )

    observed = math.fsum(window_values) / n_window_valid
    baseline_mean, baseline_sd = _baseline_stats(baseline_values)
    flagged, deviation, deviation_in_sd, direction = _band_decision(
        observed=observed,
        baseline_mean=baseline_mean,
        baseline_sd=baseline_sd,
        band_sd=band_sd,
    )
    confidence = min(
        1.0, n_window_valid / window_days, n_baseline_valid / baseline_days
    )
    if baseline_sd > 0.0:
        sd_note = f"deviation {deviation:+.4f} = {deviation_in_sd:+.3f} SD"
    else:
        sd_note = (
            f"deviation {deviation:+.4f} with SD = 0: the band collapses to "
            "the baseline mean, deviation-in-SD undefined"
        )
    detail = (
        f"HRV: {window_days}-day rolling ln(rMSSD) mean {observed:.4f} vs "
        f"{baseline_days}-day baseline mean {baseline_mean:.4f} (SD "
        f"{baseline_sd:.4f}, {n_baseline_valid} valid days); band ± {band_sd:g} SD; "
        f"{sd_note}; flagged={flagged}; direction {direction!r}; confidence "
        f"{confidence:.2f}"
    )
    return ReadinessSignal(
        key=SIGNAL_KEY_HRV,
        status="assessed",
        direction=direction,
        confidence=confidence,
        observed=observed,
        baseline_mean=baseline_mean,
        baseline_sd=baseline_sd,
        deviation=deviation,
        deviation_in_sd=deviation_in_sd,
        flagged=flagged,
        band_half_width_sd=band_sd,
        window_days=window_days,
        baseline_days=baseline_days,
        n_window_valid=n_window_valid,
        n_baseline_valid=n_baseline_valid,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Resting-HR and sleep signals (RID-2)
# ---------------------------------------------------------------------------


def _point_observation_signal(
    key: SignalKey,
    series: Mapping[dt.date, float | None],
    *,
    quantity: str,
    baseline_days: int,
    band_sd: float,
    min_baseline_valid_days: int,
) -> ReadinessSignal:
    """Shared resting-HR / sleep logic: as-of observation vs own baseline.

    The observed value is the measurement ON the as-of date (the mapping's
    last date); the baseline is the ``baseline_days`` calendar days
    immediately preceding it, EXCLUSIVE of the as-of day (the observed day
    never contributes to its own baseline). Missing as-of observation or a
    short baseline history is explicit ``insufficient_data``.
    """
    _validate_baseline_params(
        baseline_days=baseline_days,
        band_sd=band_sd,
        min_baseline_valid_days=min_baseline_valid_days,
    )
    entries = _validated_daily_series(series, quantity=quantity, positive=True)

    as_of, observed_value = entries[-1]
    baseline_entries = entries[-(baseline_days + 1) : -1]
    baseline_values = [value for _, value in baseline_entries if value is not None]
    n_baseline_valid = len(baseline_values)

    if observed_value is None:
        return _insufficient_signal(
            key,
            detail=(
                f"{quantity} observation missing on the as-of date "
                f"{as_of.isoformat()}: reported as missing, never substituted"
            ),
            band_sd=band_sd,
            window_days=1,
            baseline_days=baseline_days,
            n_window_valid=0,
            n_baseline_valid=n_baseline_valid,
        )
    if n_baseline_valid < min_baseline_valid_days:
        return _insufficient_signal(
            key,
            detail=(
                f"{quantity} baseline history short: requires "
                f"{min_baseline_valid_days} of {baseline_days} baseline days "
                f"with a measurement before {as_of.isoformat()}, found "
                f"{n_baseline_valid}; a missing day is reported, never substituted"
            ),
            band_sd=band_sd,
            window_days=1,
            baseline_days=baseline_days,
            n_window_valid=1,
            n_baseline_valid=n_baseline_valid,
        )

    observed = observed_value
    baseline_mean, baseline_sd = _baseline_stats(baseline_values)
    flagged, deviation, deviation_in_sd, direction = _band_decision(
        observed=observed,
        baseline_mean=baseline_mean,
        baseline_sd=baseline_sd,
        band_sd=band_sd,
    )
    confidence = min(1.0, n_baseline_valid / baseline_days)
    if baseline_sd > 0.0:
        sd_note = f"deviation {deviation:+.4g} = {deviation_in_sd:+.3f} SD"
    else:
        sd_note = (
            f"deviation {deviation:+.4g} with SD = 0: the band collapses to "
            "the baseline mean, deviation-in-SD undefined"
        )
    detail = (
        f"{quantity}: observed {observed:.4g} on {as_of.isoformat()} vs "
        f"{baseline_days}-day baseline mean {baseline_mean:.4g} (SD "
        f"{baseline_sd:.4g}, {n_baseline_valid} valid days); band ± {band_sd:g} SD; "
        f"{sd_note}; flagged={flagged}; direction {direction!r}; confidence "
        f"{confidence:.2f}"
    )
    return ReadinessSignal(
        key=key,
        status="assessed",
        direction=direction,
        confidence=confidence,
        observed=observed,
        baseline_mean=baseline_mean,
        baseline_sd=baseline_sd,
        deviation=deviation,
        deviation_in_sd=deviation_in_sd,
        flagged=flagged,
        band_half_width_sd=band_sd,
        window_days=1,
        baseline_days=baseline_days,
        n_window_valid=1,
        n_baseline_valid=n_baseline_valid,
        detail=detail,
    )


def resting_hr_readiness(
    series: Mapping[dt.date, float | None],
    *,
    baseline_days: int = DEFAULT_RHR_BASELINE_DAYS,
    band_sd: float = DEFAULT_RHR_BAND_SD,
    min_baseline_valid_days: int = DEFAULT_RHR_MIN_BASELINE_VALID_DAYS,
) -> ReadinessSignal:
    """Resting-HR readiness: the as-of value vs the 30-day own baseline.

    Formula (section 7.4):

        observed = the daily resting HR on the as-of date (the mapping's
                   last date)
        baseline = mean and SAMPLE SD (ddof=1) of the ``baseline_days``
                   (default 30) daily values immediately preceding it,
                   EXCLUSIVE of the as-of day
        flagged  = observed strictly outside baseline ± ``band_sd``
                   (default 0.5) x baseline SD

    Direction vocabulary ``"elevated"`` / ``"normal"`` / ``"low"`` describes
    the observed VALUE relative to the band (elevated resting HR is the
    classic concern, but the interpretation belongs to the warning-rule
    layer, RID-3). The 0.5 SD band default is a documented OWNER CHOICE
    (SWC logic extended to resting HR, see the module docstring). Values
    must be positive bpm; a missing as-of observation or a short baseline
    history is explicit ``insufficient_data``.
    """
    return _point_observation_signal(
        SIGNAL_KEY_RHR,
        series,
        quantity="resting heart rate",
        baseline_days=baseline_days,
        band_sd=band_sd,
        min_baseline_valid_days=min_baseline_valid_days,
    )


def sleep_readiness(
    series: Mapping[dt.date, float | None],
    *,
    baseline_days: int = DEFAULT_SLEEP_BASELINE_DAYS,
    band_sd: float = DEFAULT_SLEEP_BAND_SD,
    min_baseline_valid_days: int = DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS,
) -> ReadinessSignal:
    """Sleep readiness: the as-of duration vs the athlete's own baseline.

    Identical treatment to :func:`resting_hr_readiness` (section 7.4
    "sleep: duration vs personal baseline"): observed value on the as-of
    date, baseline over the previous ``baseline_days`` (default 30, OWNER
    CHOICE — the brief fixes no sleep window) days excluding the as-of day,
    strict ± ``band_sd`` (default 0.5 SD, OWNER CHOICE) band, sample SD
    (ddof=1). The unit is whatever the caller passes consistently (e.g.
    hours); the engine is unit-agnostic. Values must be positive; a missing
    as-of observation or a short baseline history is explicit
    ``insufficient_data``.
    """
    return _point_observation_signal(
        SIGNAL_KEY_SLEEP,
        series,
        quantity="sleep duration",
        baseline_days=baseline_days,
        band_sd=band_sd,
        min_baseline_valid_days=min_baseline_valid_days,
    )


def _readiness_reason(signal: ReadinessSignal) -> str:
    """One human-readable reason line for an assessed, adverse signal."""
    observed = signal.observed
    baseline_mean = signal.baseline_mean
    deviation_in_sd = signal.deviation_in_sd
    if observed is None or baseline_mean is None:
        # Unreachable for assessed signals (they always carry values);
        # kept defensive so the reason is never a substituted number.
        return f"{_SIGNAL_LABELS[signal.key]} is outside its baseline band."
    comparison = "below" if signal.direction == "low" else "above"
    if deviation_in_sd is None:
        return (
            f"{_SIGNAL_LABELS[signal.key]} is {comparison} its baseline band: "
            f"observed {observed:.4g} vs baseline mean {baseline_mean:.4g}."
        )
    return (
        f"{_SIGNAL_LABELS[signal.key]} is {comparison} its baseline band: "
        f"observed {observed:.4g} vs baseline mean {baseline_mean:.4g} "
        f"({deviation_in_sd:+.3f} SD)."
    )


def readiness_assessment(
    signals: Sequence[ReadinessSignal],
    *,
    tsb: float,
    subjective_fatigue_reported: bool | None = None,
    acwr: float | None = None,
    tsb_very_negative_below: float = DEFAULT_TSB_VERY_NEGATIVE,
) -> ReadinessAssessment:
    """Multi-signal warning rule (RID-3): suggest reducing intensity only
    when two or more adverse signals agree (section 7.4, section 12.5
    acceptance criterion).

    Inputs
    ------
    ``signals``
        Exactly one :class:`ReadinessSignal` per key of
        :data:`SIGNAL_KEYS` — the outputs of :func:`hrv_readiness`,
        :func:`resting_hr_readiness` and :func:`sleep_readiness` for the
        as-of date (missing keys, duplicates and unknown keys raise
        ``ValueError``).
    ``tsb``
        Today's Training Stress Balance from the load model
        (``app.engine.pmc``, section 7.4): "TSB very negative" counts as
        an adverse signal when STRICTLY below
        ``tsb_very_negative_below`` (default :data:`DEFAULT_TSB_VERY_NEGATIVE`,
        OWNER CHOICE -10.0 — provenance in the module docstring). Exactly
        AT the threshold does NOT count (tolerant boundary comparison,
        the ZON-9 margin precedent).
    ``subjective_fatigue_reported``
        ``True`` when the athlete reported fatigue today (section 7.4's
        example context signal); ``None`` / ``False`` = not reported.
    ``acwr``
        CONTEXT ONLY (section 7.2, Impellizzeri et al. 2020): carried
        through to the result verbatim and never consulted for the
        verdict — changing it cannot change the suggestion or the
        agreement count (asserted by tests).

    Adverse semantics
    -----------------
    HRV direction ``"low"``, resting-HR direction ``"elevated"`` and sleep
    direction ``"low"`` are adverse (:data:`ADVERSE_DIRECTIONS`); the
    opposite directions are not. A signal with
    ``status == "insufficient_data"`` NEVER counts as agreeing — absence
    of evidence is not evidence. Subjective fatigue counts when reported.
    The suggestion fires exactly when ``agreement_count >= 2`` and is a
    "consider reducing intensity" statement, never a diagnosis or medical
    advice (section 3); when every warning signal in
    :data:`WARNING_SIGNAL_KEYS` is adverse the suggestion additionally
    proposes rest and recovery (documented OWNER CHOICE, still a
    suggestion).

    Returns a frozen :class:`ReadinessAssessment` (RID-4): the structured
    multi-signal object with NO composite score, wrapped by the future
    ``get_readiness`` tool (Feature 6). Raises ``ValueError`` on a
    missing/duplicate/unknown signal key or a non-finite ``tsb``,
    ``tsb_very_negative_below`` or ``acwr``.
    """
    if not math.isfinite(tsb):
        raise ValueError(f"tsb must be finite, got {tsb!r}")
    if not math.isfinite(tsb_very_negative_below):
        raise ValueError(
            f"tsb_very_negative_below must be finite, got {tsb_very_negative_below!r}"
        )
    if acwr is not None and not math.isfinite(acwr):
        raise ValueError(f"acwr must be finite, got {acwr!r}")

    by_key: dict[SignalKey, ReadinessSignal] = {}
    for signal in signals:
        if signal.key not in SIGNAL_KEYS:
            raise ValueError(f"unknown readiness signal key: {signal.key!r}")
        if signal.key in by_key:
            raise ValueError(f"duplicate readiness signal key: {signal.key!r}")
        by_key[signal.key] = signal
    missing = [key for key in SIGNAL_KEYS if key not in by_key]
    if missing:
        raise ValueError(
            f"missing readiness signal keys: {missing!r} — every signal of "
            "SIGNAL_KEYS must be passed, use its insufficient_data result "
            "when the signal cannot be assessed"
        )

    adverse: list[WarningSignalKey] = []
    reasons: list[str] = []
    for key in SIGNAL_KEYS:
        signal = by_key[key]
        # An insufficient_data signal never counts as agreeing: absence of
        # evidence is not evidence (asserted by tests).
        if signal.status == "assessed" and signal.direction == ADVERSE_DIRECTIONS[key]:
            adverse.append(key)
            reasons.append(_readiness_reason(signal))
    if tsb < tsb_very_negative_below and not _same_boundary(
        tsb, tsb_very_negative_below
    ):
        adverse.append("tsb_very_negative")
        reasons.append(
            f"TSB is {tsb:+.1f}, below the very-negative threshold "
            f"{tsb_very_negative_below:+.1f}: recent fatigue exceeds "
            "chronic fitness."
        )
    if subjective_fatigue_reported is True:
        adverse.append("subjective_fatigue")
        reasons.append("Subjective fatigue was reported for today.")

    agreement_count = len(adverse)
    suggest_reduce_intensity = agreement_count >= 2
    suggestion: str | None = None
    if suggest_reduce_intensity:
        suggestion = (
            SUGGESTION_REDUCE_AND_REST
            if agreement_count == len(WARNING_SIGNAL_KEYS)
            else SUGGESTION_REDUCE_INTENSITY
        )
    return ReadinessAssessment(
        signals=tuple(by_key[key] for key in SIGNAL_KEYS),
        adverse_signal_keys=tuple(adverse),
        agreement_count=agreement_count,
        suggest_reduce_intensity=suggest_reduce_intensity,
        suggestion=suggestion,
        reasons=tuple(reasons),
        tsb=tsb,
        tsb_very_negative_below=tsb_very_negative_below,
        subjective_fatigue_reported=subjective_fatigue_reported,
        acwr=acwr,
    )
