"""Banister impulse-response performance model (fitness-fatigue).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core`` (PROJECT_BRIEF sections 6 and 14). Daily loads come in as an
explicit ``Mapping[date, float]`` (one entry per training session's calendar
day) and performance markers as an explicit ``Mapping[date, float]`` (measured
performance values, e.g. race results or test efforts on any fixed scale
where higher is better); side effects, persistence and settings reads belong
in callers. ``scipy.optimize`` is a pure computation library and is the only
third-party import.

Formulas and references
-----------------------
Banister impulse-response model (Banister 1991 "Modeling elite athletic
performance"; Morton, Fitz-Clarke & Banister 1990 "Modeling human performance
in running", J Appl Physiol 69(3):1171-1177):

    p(t) = p0 + k1 * SUM_s w(s) * e^(-(t-s)/tau1)
              - k2 * SUM_s w(s) * e^(-(t-s)/tau2)

where the sums run over all training sessions s on or before day t with
daily load ``w(s)`` (TSS-equivalent units), ``p0`` is the zero-load baseline
performance, ``tau1`` is the fitness time constant (default 42 days) and
``tau2`` is the fatigue time constant (default 7 days). ``k1 >= 0`` and
``k2 >= 0`` are positive gains (a negative gain is unphysical and rejected).

Causal semantics: only sessions with ``s <= t`` contribute; a session after
the target date never leaks into the past (``e^(-(t-s)/tau)`` is defined only
for ``t >= s``). A session ON the target date contributes with full weight
(``t - s = 0``, ``e^0 = 1``). Unlike the PMC recursions in ``app.engine.pmc``,
calendar gaps in the load series are harmless here: the decay depends only on
the date difference ``(t - s).days``, so an absent day contributes nothing by
construction - but an *empty* load mapping is still rejected, because it
cannot be distinguished from missing data (a caller bug must not silently
degrade to ``p(t) = p0``).

Personalization guard (the core invariant, PROJECT_BRIEF section 7.2)
---------------------------------------------------------------------
The model is only "personalized" when its parameters were FITTED to this
athlete's own performance markers with reported fit quality. Consequently
:func:`fit_banister` refuses to fit - and returns an explicitly
non-personalized result - unless the marker series has at least
``min_markers`` entries (default :data:`DEFAULT_MIN_MARKERS`):

- default ``min_markers = 10 = 2 * 5``: the richest configuration fits five
  free parameters (p0, k1, k2, tau1, tau2), so ten markers guarantee at least
  five residual degrees of freedom even there; below that a Banister fit is
  under-determined and fits noise. The threshold is an explicit, overridable
  parameter (documented owner choice, section 14).
- an insufficient (or failed) fit returns ``personalized=False`` with a
  machine-readable ``reason`` string and ``parameters=None``: a caller
  CANNOT present unfitted parameters as personalized, because there are
  none to present.
- the reason strings are the exact constants :data:`REASON_FITTED`,
  :data:`REASON_INSUFFICIENT_MARKERS` and :data:`REASON_FIT_FAILED` -
  machine-readable, never prose.

Owner reality: the owner currently has NO performance markers at all (no
race results or test efforts recorded), so the unfitted
``REASON_INSUFFICIENT_MARKERS`` path is the one that will actually run today.
It is the safe default: no fit, no parameters, nothing to mis-present. Do
not wire fitted Banister predictions into reports until a real, quality-
reported fit exists.

Fit quality: a fitted result reports :class:`BanisterFitQuality` with R^2,
RMSE and the marker count, computed on the marker residuals
(predicted p(t_i) vs measured marker values). Identical marker values make
R^2 undefined (SS_tot = 0) and are rejected with ``ValueError`` instead of
degrading silently. Initial guesses are scale-aware and documented in
:func:`fit_banister`; ``p0`` is fitted jointly with k1/k2 (it is the
zero-load baseline and not identifiable from the markers otherwise);
tau1/tau2 are fitted only when ``fit_time_constants=True`` and are otherwise
held at the supplied (default 42/7) values.

All functions are pure and fully typed.

Reference: Banister 1991; Morton, Fitz-Clarke & Banister 1990.
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# scipy ships incomplete inline typing; mypy asks for the external
# ``scipy-stubs`` package, which is a dependency decision owned by LOAD-11,
# so the import is ignored here instead of adding a dev dependency.
from scipy.optimize import least_squares  # type: ignore[import-untyped]

__all__ = [
    "DEFAULT_MIN_MARKERS",
    "DEFAULT_TAU1_DAYS",
    "DEFAULT_TAU2_DAYS",
    "MAX_FIT_TAU_DAYS",
    "MIN_FIT_TAU_DAYS",
    "REASON_FITTED",
    "REASON_FIT_FAILED",
    "REASON_INSUFFICIENT_MARKERS",
    "BanisterFit",
    "BanisterFitQuality",
    "BanisterParameters",
    "evaluate_banister",
    "fit_banister",
]

DEFAULT_TAU1_DAYS: Final[float] = 42.0
"""Default fitness time constant: tau1 = 42 days (Banister model)."""

DEFAULT_TAU2_DAYS: Final[float] = 7.0
"""Default fatigue time constant: tau2 = 7 days (Banister model)."""

DEFAULT_MIN_MARKERS: Final[int] = 10
"""Default minimum performance-marker count before a fit is attempted.

``10 = 2 * 5``: the richest fit has five free parameters (p0, k1, k2, tau1,
tau2), so ten markers leave at least five residual degrees of freedom even
in that configuration; below it the fit is under-determined and models
noise, never the athlete (documented owner choice, PROJECT_BRIEF section 14).
"""

MIN_FIT_TAU_DAYS: Final[float] = 1.0
"""Lower bound for fitted time constants (days): anything below 1 day is
not a training-adaptation time scale."""

MAX_FIT_TAU_DAYS: Final[float] = 365.0
"""Upper bound for fitted time constants (days): a Banister time constant
beyond one year is not identifiable from a realistic marker series."""

REASON_FITTED: Final[str] = "fitted"
"""Machine-readable reason: a quality-reported fit to the athlete's markers."""

REASON_INSUFFICIENT_MARKERS: Final[str] = "insufficient_markers"
"""Machine-readable reason: fewer than ``min_markers`` performance markers,
so no fit was attempted. This is today's default for the owner (no markers
recorded yet) and must never be presented as personalized."""

REASON_FIT_FAILED: Final[str] = "fit_failed"
"""Machine-readable reason: the optimizer did not converge; no parameters
are reported and the result is never personalized."""


@dataclass(frozen=True, slots=True)
class BanisterParameters:
    """One parameter set of the Banister impulse-response model.

    ``p0`` is the zero-load baseline performance, ``k1``/``k2`` the positive
    fitness/fatigue gains and ``tau1_days``/``tau2_days`` the fitness/fatigue
    time constants in days.
    """

    p0: float
    k1: float
    k2: float
    tau1_days: float
    tau2_days: float


@dataclass(frozen=True, slots=True)
class BanisterFitQuality:
    """Fit quality over the performance markers used for the fit.

    ``r2`` is the coefficient of determination ``1 - SS_res / SS_tot`` and
    ``rmse`` the root-mean-square residual of ``p(t_i)`` against the measured
    marker values; ``marker_count`` is the number of markers fitted.
    """

    r2: float
    rmse: float
    marker_count: int


@dataclass(frozen=True, slots=True)
class BanisterFit:
    """Result of a Banister fit attempt, with the personalization guard.

    Invariant: ``parameters`` is not ``None`` if and only if
    ``personalized`` is ``True`` (``reason == REASON_FITTED``). An unfitted
    result carries ``parameters=None`` and ``quality=None`` so no caller can
    present it as a personalized model (PROJECT_BRIEF section 7.2).
    """

    personalized: bool
    reason: str
    parameters: BanisterParameters | None
    quality: BanisterFitQuality | None


def _validated_load_items(loads: Mapping[dt.date, float]) -> list[tuple[dt.date, float]]:
    """Validate a session-load mapping and return it as sorted items.

    Raises ``ValueError`` for an empty mapping (cannot be distinguished from
    missing data) or a negative load. Calendar gaps are intentionally NOT an
    error here: the decay kernel depends only on ``(t - s).days``, so an
    absent day contributes nothing by construction.
    """
    if not loads:
        raise ValueError(
            "load series is empty: provide at least one session day (a "
            "rest day needs no entry, but an empty series is indistinguishable "
            "from missing data)"
        )
    items = sorted(loads.items())
    for day, load in items:
        if load < 0.0:
            raise ValueError(
                f"negative session load on {day}: {load!r} (load is non-negative)"
            )
    return items


def _validate_time_constants(*taus: tuple[str, float]) -> None:
    """Reject non-positive time constants (misconfiguration, never a default)."""
    for name, tau in taus:
        if tau <= 0.0:
            raise ValueError(f"{name} must be positive, got {tau!r} days")


def _validate_gains(*gains: tuple[str, float]) -> None:
    """Reject negative or non-finite gains (unphysical in the model)."""
    for name, value in gains:
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
        if value < 0.0:
            raise ValueError(f"{name} must be non-negative, got {value!r}")


def _response_sums(
    items: list[tuple[dt.date, float]],
    target: dt.date,
    tau1_days: float,
    tau2_days: float,
) -> tuple[float, float]:
    """Fitness and fatigue response sums at ``target`` (causal, ``s <= t``)."""
    fitness: list[float] = []
    fatigue: list[float] = []
    for session_day, load in items:
        if session_day > target:
            continue
        elapsed = (target - session_day).days
        fitness.append(load * math.exp(-elapsed / tau1_days))
        fatigue.append(load * math.exp(-elapsed / tau2_days))
    return math.fsum(fitness), math.fsum(fatigue)


def evaluate_banister(
    loads: Mapping[dt.date, float],
    *,
    target_date: dt.date,
    p0: float,
    k1: float,
    k2: float,
    tau1_days: float = DEFAULT_TAU1_DAYS,
    tau2_days: float = DEFAULT_TAU2_DAYS,
) -> float:
    """Evaluate the Banister performance ``p(t)`` for SUPPLIED parameters.

    Formula (Banister 1991; Morton, Fitz-Clarke & Banister 1990):

        p(t) = p0 + k1 * SUM_s w(s) e^(-(t-s)/tau1)
                   - k2 * SUM_s w(s) e^(-(t-s)/tau2)

    Pure evaluation - no fitting happens here. Defaults ``tau1_days=42`` and
    ``tau2_days=7`` (overridable). Only sessions on or before ``target_date``
    contribute (causal model); a session on the target date has full weight.

    Validation (``ValueError``): empty load series, negative session load,
    non-positive time constants, negative or non-finite k1/k2, non-finite
    p0.

    To obtain fitted parameters use :func:`fit_banister`; only a result with
    ``personalized=True`` may be presented as personalized.

    Reference: Banister 1991; Morton, Fitz-Clarke & Banister 1990.
    """
    _validate_time_constants(("tau1_days", tau1_days), ("tau2_days", tau2_days))
    _validate_gains(("k1", k1), ("k2", k2))
    if not math.isfinite(p0):
        raise ValueError(f"p0 must be finite, got {p0!r}")
    items = _validated_load_items(loads)
    fitness_sum, fatigue_sum = _response_sums(items, target_date, tau1_days, tau2_days)
    return p0 + k1 * fitness_sum - k2 * fatigue_sum


def _fit_quality(
    predicted: Sequence[float], actual: Sequence[float], marker_count: int
) -> BanisterFitQuality:
    """R^2 / RMSE over the marker residuals (see module docstring)."""
    residuals = [p - a for p, a in zip(predicted, actual, strict=True)]
    ss_res = math.fsum(r * r for r in residuals)
    mean_actual = math.fsum(actual) / len(actual)
    ss_tot = math.fsum((a - mean_actual) ** 2 for a in actual)
    if ss_tot == 0.0:
        raise ValueError(
            "performance markers have zero variance: R^2 is undefined "
            "(all measured values identical)"
        )
    return BanisterFitQuality(
        r2=1.0 - ss_res / ss_tot,
        rmse=math.sqrt(ss_res / len(residuals)),
        marker_count=marker_count,
    )


def fit_banister(
    loads: Mapping[dt.date, float],
    markers: Mapping[dt.date, float],
    *,
    fit_time_constants: bool = False,
    min_markers: int = DEFAULT_MIN_MARKERS,
    tau1_days: float = DEFAULT_TAU1_DAYS,
    tau2_days: float = DEFAULT_TAU2_DAYS,
) -> BanisterFit:
    """Fit the Banister model - ONLY with enough performance markers.

    Fits ``p0``, ``k1`` and ``k2`` (least squares, ``scipy.optimize``) of

        p(t) = p0 + k1 * SUM w(s) e^(-(t-s)/tau1)
                   - k2 * SUM w(s) e^(-(t-s)/tau2)

    against the performance markers; with ``fit_time_constants=True`` the
    time constants ``tau1``/``tau2`` are fitted too (bounded to
    ``[MIN_FIT_TAU_DAYS, MAX_FIT_TAU_DAYS]`` days, initial guesses
    ``tau1_days``/``tau2_days``), otherwise they stay at the supplied values
    (defaults 42/7). ``p0`` is always fitted jointly: it is the zero-load
    baseline and not identifiable from the markers otherwise.

    Guard (PROJECT_BRIEF section 7.2): if the marker series has fewer than
    ``min_markers`` entries (default 10, see :data:`DEFAULT_MIN_MARKERS`)
    NO fit is attempted and the result is
    ``personalized=False, reason="insufficient_markers", parameters=None,
    quality=None``. The owner currently has no performance markers at all,
    so this unfitted path is today's safe default - it must never be
    presented as personalized. A non-converging optimizer run yields
    ``reason="fit_failed"`` on the same unfitted shape.

    Fit quality (only on the fitted path): :class:`BanisterFitQuality` with
    R^2, RMSE and the marker count over the marker residuals. Identical
    marker values (zero variance, R^2 undefined) raise ``ValueError``.

    Validation (``ValueError``): empty load series, negative load,
    non-finite marker value, non-positive ``min_markers`` or time constants.
    Initial guesses: ``p0`` = minimum marker value; ``k1``/``k2`` = marker
    value range divided by the largest response sum (scale-aware).

    Reference: Banister 1991; Morton, Fitz-Clarke & Banister 1990.
    """
    _validate_time_constants(("tau1_days", tau1_days), ("tau2_days", tau2_days))
    if min_markers <= 0:
        raise ValueError(f"min_markers must be positive, got {min_markers!r}")
    marker_items = sorted(markers.items())
    for marker_day, value in marker_items:
        if not math.isfinite(value):
            raise ValueError(
                f"performance marker on {marker_day} must be finite, got {value!r}"
            )
    items = _validated_load_items(loads)

    if len(marker_items) < min_markers:
        return BanisterFit(
            personalized=False,
            reason=REASON_INSUFFICIENT_MARKERS,
            parameters=None,
            quality=None,
        )

    actual = [value for _, value in marker_items]
    marker_days = [day for day, _ in marker_items]
    value_range = max(actual) - min(actual)
    if value_range == 0.0:
        raise ValueError(
            "performance markers have zero variance: R^2 is undefined "
            "(all measured values identical)"
        )

    def residual(params: Sequence[float]) -> list[float]:
        p0, k1, k2 = params[0], params[1], params[2]
        t1 = params[3] if fit_time_constants else tau1_days
        t2 = params[4] if fit_time_constants else tau2_days
        predicted = [
            p0 + k1 * fit_s - k2 * fat_s
            for fit_s, fat_s in (
                _response_sums(items, day, t1, t2) for day in marker_days
            )
        ]
        return [p - a for p, a in zip(predicted, actual, strict=True)]

    response_at_days = [
        _response_sums(items, day, tau1_days, tau2_days) for day in marker_days
    ]
    max_fitness = max(fit_s for fit_s, _ in response_at_days)
    max_fatigue = max(fat_s for _, fat_s in response_at_days)
    x0 = [
        min(actual),
        value_range / max(max_fitness, 1e-12),
        value_range / max(max_fatigue, 1e-12),
    ]
    lower = [-math.inf, 0.0, 0.0]
    upper = [math.inf, math.inf, math.inf]
    if fit_time_constants:
        x0 += [tau1_days, tau2_days]
        lower += [MIN_FIT_TAU_DAYS, MIN_FIT_TAU_DAYS]
        upper += [MAX_FIT_TAU_DAYS, MAX_FIT_TAU_DAYS]

    result = least_squares(residual, x0, bounds=(lower, upper), method="trf")
    if not result.success:
        return BanisterFit(
            personalized=False,
            reason=REASON_FIT_FAILED,
            parameters=None,
            quality=None,
        )

    p0_fit, k1_fit, k2_fit = float(result.x[0]), float(result.x[1]), float(result.x[2])
    t1_fit = float(result.x[3]) if fit_time_constants else tau1_days
    t2_fit = float(result.x[4]) if fit_time_constants else tau2_days
    predicted = [
        p0_fit + k1_fit * fit_s - k2_fit * fat_s
        for fit_s, fat_s in (
            _response_sums(items, day, t1_fit, t2_fit) for day in marker_days
        )
    ]
    return BanisterFit(
        personalized=True,
        reason=REASON_FITTED,
        parameters=BanisterParameters(
            p0=p0_fit, k1=k1_fit, k2=k2_fit, tau1_days=t1_fit, tau2_days=t2_fit
        ),
        quality=_fit_quality(predicted, actual, len(marker_items)),
    )
