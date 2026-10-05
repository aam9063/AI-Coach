"""Mean-maximal power curve and bike Critical Power (CP/W') fitting.

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core.settings`` (PROJECT_BRIEF sections 6 and 14). Thresholds are
explicit parameters; side effects and settings reads belong in callers.

Real-data limitation (ZON-1/ZON-2, ODD ``engine-zones.md``): the owner has
NO power meter, so there is no real power stream to fit. Per PROJECT_BRIEF
section 12.4 the acceptance for the CP/W' fit is validation on SYNTHETIC
data with known parameters (``tests/engine/test_zones_cp.py``), and every
result computed from a real stream later must be treated accordingly.

Formulas and references
-----------------------
Mean-maximal power (MMP) curve: for each duration ``d``, the best (highest)
mean power over any contiguous window of ``d`` seconds in the per-second
power stream. This is the standard "mean-maximal power" / "best efforts"
curve of power-meter analysis (Allen & Coggan, "Training and Racing with a
Power Meter"); it is the input required by PROJECT_BRIEF section 7.3.

Critical Power (Monod & Scherrer 1965, "La charge anaerobie"; Jones et al.
2019, "Severe Domain" review, Med Sci Sports Exerc): from the MMP curve,
take the best efforts between 2 and 20 minutes and fit the linear
2-parameter model in its work form:

    Work(J) = CP(W) * t(s) + W'(J)

where ``Work = P(t) * t`` for each curve point. CP (W) is the slope — the
theoretical power that could be sustained indefinitely — and W' (J) is the
curvature constant — the finite work capacity above CP. The fit is plain
ordinary least squares on the ``(t, Work)`` points (closed form; no
iterative optimiser needed for the linear form). Fit quality is reported
as R^2 and RMSE (in joules, on the Work axis) plus the number of curve
points used, so callers can judge — and later gate threshold proposals
(ZON-9) on — how well the athlete's data matches the 2-parameter model.

Purity: all functions are pure and fully typed; validation errors raise
``ValueError`` rather than clamping or silently defaulting.

Missing-sample (gap) rule
-------------------------
Power streams are per-second sample sequences with ``None`` entries marking
sensor gaps (the ingest parser's convention, mirroring
``app.engine.load``). For each rolling window of ``w = d / sample_interval``
samples:

- A window QUALIFIES only when ``valid_count / w >= min_valid_fraction``
  (default 1.0: only fully complete windows count; owners may relax the
  parameter explicitly for gap-heavy streams).
- A qualifying window's mean power is the arithmetic mean over its VALID
  (non-``None``) samples only — the same convention as
  :func:`app.engine.load.normalized_power` (gaps are excluded, never
  treated as zero watts).
- A duration with no qualifying window — including durations longer than
  the stream (the window never fills) and durations shorter than one
  sample interval — is simply ABSENT from the result. Absence is the
  documented "not measurable" signal; no degraded fallback value is
  invented.

Complexity: for a stream of ``n`` samples and ``k`` requested durations,
:func:`mean_maximal_power_curve` runs in O(k * n) time and O(n) memory
(one shared valid-sample prefix sum; then one O(n) rolling-window pass per
duration). A 3-hour 1 Hz stream (~10,800 samples) with the default 9
durations is ~97,000 window evaluations and completes in milliseconds.

Configurable constants (ZON-11, §7/§14)
---------------------------------------
Every constant below is a function parameter whose default is the
documented module constant (the documented fallback); ZON-11 will move the
effective values to Settings (``app.core.settings`` fields ``engine_*``)
like the load constants were — this module never imports settings (§6
purity):

- CP fit window bounds 2 min (120 s) and 20 min (1200 s): the published
  recommendation for the linear CP model (Jones et al. 2019; PROJECT_BRIEF
  section 7.3) — LITERATURE reference, not an owner choice;
  :func:`fit_critical_power` parameters ``min_duration_s`` /
  ``max_duration_s``.
- MMP duration set ``(1, 60, 120, 300, 480, 600, 1200, 1800, 3600)`` s
  (1 s, 1, 2, 5, 8, 10, 20, 30, 60 min): OWNER CHOICE — a fixed,
  chart-friendly ladder spanning sprint to hour power; the 20 min point
  feeds the FTP-as-95%-of-20-min rule (ZON-3) and the 2-20 min points feed
  the CP fit.
- MMP ``min_valid_fraction`` default 1.0 (strict: only fully complete
  windows count): OWNER CHOICE, mirroring the strict NP default in
  ``app.engine.load``; relax explicitly for gap-heavy streams.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CP_MAX_DURATION_S",
    "CP_MIN_DURATION_S",
    "DEFAULT_MMP_DURATIONS_S",
    "CriticalPowerFit",
    "fit_critical_power",
    "mean_maximal_power_curve",
]


CP_MIN_DURATION_S: Final[float] = 120.0
"""Lower bound of the CP fit window: 2 minutes (Jones et al. 2019).

LITERATURE constant (the 2-20 min window for the linear CP model); may move
to Settings with ZON-11.
"""

CP_MAX_DURATION_S: Final[float] = 1200.0
"""Upper bound of the CP fit window: 20 minutes (Jones et al. 2019).

LITERATURE constant; may move to Settings with ZON-11.
"""

DEFAULT_MMP_DURATIONS_S: Final[tuple[int, ...]] = (
    1,
    60,
    120,
    300,
    480,
    600,
    1200,
    1800,
    3600,
)
"""Default MMP durations in seconds: 1 s, 1, 2, 5, 8, 10, 20, 30, 60 min.

OWNER CHOICE — a fixed chart-friendly ladder spanning sprint to hour power;
the 20 min point feeds the FTP-as-95%-of-best-20-min rule (ZON-3) and the
2-20 min points feed the CP fit. May move to Settings with ZON-11.
"""


def mean_maximal_power_curve(
    power_samples: Sequence[float | None],
    *,
    sample_interval_s: float = 1.0,
    durations_s: Sequence[int] | None = None,
    min_valid_fraction: float = 1.0,
) -> dict[int, float]:
    """Best mean power over each requested duration (mean-maximal power curve).

    For each duration ``d`` the function scans every contiguous window of
    ``w = round(d / sample_interval_s)`` samples (rolling window, 1-sample
    step), keeps the windows that qualify under the gap rule, and reports
    the highest window mean power. Durations with no qualifying window are
    absent from the result (see the module docstring for the full gap rule
    and the complexity note: O(k * n) time, O(n) memory).

    ``power_samples`` is the per-second ingest-parser sequence (``None``
    marks a stream gap). An empty or all-``None`` sequence raises
    ``ValueError`` ("no usable power data"); ``sample_interval_s <= 0``,
    ``min_valid_fraction`` outside ``(0, 1]``, a non-positive duration, or
    an empty ``durations_s`` also raise ``ValueError``.

    Parameters mirror the module's configurable constants (ZON-11):
    ``durations_s`` defaults to :data:`DEFAULT_MMP_DURATIONS_S` (owner
    choice), ``min_valid_fraction`` to the strict 1.0 (owner choice).

    Reference: Allen & Coggan, "Training and Racing with a Power Meter"
    (mean-maximal power curve).
    """
    if sample_interval_s <= 0.0:
        raise ValueError(
            f"sample_interval must be positive, got {sample_interval_s!r} seconds"
        )
    if not 0.0 < min_valid_fraction <= 1.0:
        raise ValueError(
            f"min_valid_fraction must be within (0, 1], got {min_valid_fraction!r}"
        )
    durations = DEFAULT_MMP_DURATIONS_S if durations_s is None else tuple(durations_s)
    if not durations:
        raise ValueError("durations_s must not be empty: no duration was requested")
    for duration in durations:
        if duration <= 0:
            raise ValueError(
                f"durations must be positive, got {duration!r} seconds"
            )
    samples = list(power_samples)
    if not any(s is not None for s in samples):
        raise ValueError(
            "no usable power data: samples sequence is empty or all missing"
        )

    # Shared valid-sample prefix sums: prefix[i] = sum of valid samples
    # before i, counts[i] = number of valid samples before i. One O(n) pass
    # makes every window sum/count an O(1) difference.
    n = len(samples)
    prefix = [0.0] * (n + 1)
    counts = [0] * (n + 1)
    for index, sample in enumerate(samples):
        prefix[index + 1] = prefix[index] + (sample if sample is not None else 0.0)
        counts[index + 1] = counts[index] + (0 if sample is None else 1)

    curve: dict[int, float] = {}
    for duration in durations:
        window = round(duration / sample_interval_s)
        if window < 1 or window > n:
            continue  # window never fills / shorter than one sample: absent
        best: float | None = None
        for start in range(n - window + 1):
            valid = counts[start + window] - counts[start]
            if valid / window < min_valid_fraction:
                continue  # window lacks enough valid samples: skipped
            total = prefix[start + window] - prefix[start]
            mean = total / valid
            if best is None or mean > best:
                best = mean
        if best is not None:
            curve[duration] = best
    return curve


@dataclass(frozen=True, slots=True)
class CriticalPowerFit:
    """Result of the linear Critical Power fit on a mean-maximal power curve.

    ``cp_watts`` is the CP slope (W), ``w_prime_joules`` the W' curvature
    constant (J), ``r_squared`` and ``rmse_joules`` the fit quality on the
    Work axis (RMSE in joules), and ``n_points`` the number of curve points
    inside the fit window that the fit used. Frozen and slotted, consistent
    with :class:`app.engine.load.BikePowerLoad` /
    :class:`app.engine.pmc.PmcSeries` result conventions.
    """

    cp_watts: float
    w_prime_joules: float
    r_squared: float
    rmse_joules: float
    n_points: int


def fit_critical_power(
    curve: Mapping[int, float],
    *,
    min_duration_s: float = CP_MIN_DURATION_S,
    max_duration_s: float = CP_MAX_DURATION_S,
) -> CriticalPowerFit:
    """Fit Work = CP * t + W' to the 2-20 min best efforts of an MMP curve.

    Curve points with ``min_duration_s <= duration <= max_duration_s``
    (defaults :data:`CP_MIN_DURATION_S` / :data:`CP_MAX_DURATION_S`, the
    literature 2-20 min window) are converted to work ``Work = P * t`` and
    fitted by ordinary least squares in closed form:

        CP  = (n * sum(t*W) - sum(t) * sum(W)) / (n * sum(t^2) - sum(t)^2)
        W'  = (sum(W) - CP * sum(t)) / n

    Fit quality: R^2 = 1 - SS_res / SS_tot and RMSE = sqrt(SS_res / n),
    both computed on the Work axis (joules), plus ``n_points``.

    Validation (``ValueError``, never silent): fewer than two curve points
    inside the window (a 2-parameter line needs at least two points);
    ``max_duration_s <= min_duration_s`` or a non-positive window bound;
    and the degenerate all-equal-Work case (SS_tot = 0, where R^2 is
    mathematically undefined). Points outside the window are ignored —
    the linear model is only valid in the severe domain (Jones et al.
    2019).

    Reference: Monod & Scherrer 1965, "La charge anaerobie" (linear
    work-time model); Jones et al. 2019 (2-20 min fit window convention).
    """
    if min_duration_s <= 0.0 or max_duration_s <= min_duration_s:
        raise ValueError(
            "fit window must satisfy 0 < min_duration_s < max_duration_s, got "
            f"min_duration_s={min_duration_s!r}, max_duration_s={max_duration_s!r}"
        )
    points = sorted(
        (float(duration), power * duration)
        for duration, power in curve.items()
        if min_duration_s <= duration <= max_duration_s
    )
    if len(points) < 2:
        raise ValueError(
            "not enough curve points to fit Critical Power: need at least two "
            f"durations within the {min_duration_s!r}-{max_duration_s!r} s window, "
            f"got {len(points)}"
        )
    ts = [t for t, _ in points]
    works = [w for _, w in points]
    n = float(len(points))
    sum_t = math.fsum(ts)
    sum_w = math.fsum(works)
    sum_tt = math.fsum(t * t for t in ts)
    sum_tw = math.fsum(t * w for t, w in points)
    denominator = n * sum_tt - sum_t * sum_t
    if denominator == 0.0:  # pragma: no cover - unreachable via Mapping keys
        raise ValueError(
            "degenerate fit: all curve durations are identical, a 2-parameter "
            "line is undetermined"
        )
    cp = (n * sum_tw - sum_t * sum_w) / denominator
    w_prime = (sum_w - cp * sum_t) / n

    mean_work = sum_w / n
    ss_res = math.fsum((w - (cp * t + w_prime)) ** 2 for t, w in points)
    ss_tot = math.fsum((w - mean_work) ** 2 for w in works)
    if ss_tot == 0.0:
        raise ValueError(
            "degenerate curve: every point has the same work, R^2 is undefined"
        )
    rmse = math.sqrt(ss_res / n)
    return CriticalPowerFit(
        cp_watts=cp,
        w_prime_joules=w_prime,
        r_squared=1.0 - ss_res / ss_tot,
        rmse_joules=rmse,
        n_points=len(points),
    )
