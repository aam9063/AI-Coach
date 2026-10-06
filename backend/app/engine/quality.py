"""Data-quality engine: activity plausibility, stream cleaning, robust
run-threshold derivation (data quality, pure engine).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core.settings`` (PROJECT_BRIEF sections 6 and 14). Every bound below
is an explicit, owner-reviewable constant or parameter — never an invented
silent default — and every rejection carries a machine-readable reason.

WHY THIS EXISTS (measured on the owner's REAL ingested history, 2026-10)
-----------------------------------------------------------------------
The owner's history (857 activities 2020-2026, 5,036 streams) contains
corrupt records that poison naive threshold fits:

1. ACTIVITY level: 9 of the 539 runs with distance+duration carry
   impossible AVERAGE paces — e.g. 5.00 km in 10:36 = 7.87 m/s
   (2:07/km) and 15.20 km in 34:36 = 7.32 m/s (2:16/km). Both exceed
   the 5 km world-record average pace (~2:11/km ≈ 7.6 m/s); no human
   sustains them over an activity. :func:`assess_activity_plausibility`
   rejects such activities BEFORE any stream or threshold work, with a
   machine-readable reason.
2. STREAM level: after filtering those activities, the numbers are STILL
   wrong — the naive best 5-minute effort came out 3:01.5/km (5.53 m/s)
   for an athlete who jogs easy runs at 6-8 min/km — because the speed
   streams carry short GPS spikes that survive activity-level averages.
   :func:`clean_speed_stream` caps implausibly FAST samples per activity.
3. STATISTIC level: the "best effort" used the MAXIMUM, the most
   outlier-sensitive statistic there is, and the median pace inside the
   threshold HR band (3:41.9/km = 4.54 m/s) came out FASTER than the best
   20-minute effort (4:17.1/km = 3.89 m/s) — physiologically impossible.
   :func:`derive_run_threshold_candidate` aggregates with the MEDIAN
   across runs and NEVER fabricates a single number when the independent
   evidence disagrees (``candidate`` / ``insufficient_data`` /
   ``contradictory_data`` outcomes with a numeric disagreement rule).

Activity-level plausibility (1)
-------------------------------
:func:`assess_activity_plausibility` judges one activity from its summary
``distance_m`` and ``duration_s`` only (no streams needed). The average
speed ``distance / duration`` is compared against the sport's band from
:data:`DEFAULT_PLAUSIBILITY_BANDS_MPS`; a missing or nonpositive field is
reported, never guessed. The bands ARE the distance/duration consistency
check: an implausible average speed is exactly a distance inconsistent
with the duration for that sport.

Band provenance (OWNER-REVIEWABLE constants — the owner can widen or
narrow any bound; nothing here is a claim about elite human performance,
it is a band for THIS age-group triathlete's history):

- ``run`` = (1.5, 6.5) m/s. Floor 1.5 m/s = 11:07/km: below the slowest
  jogging pace (the owner's easy runs sit at 6-8 min/km = 2.1-2.8 m/s);
  a "run" averaging walking pace is corrupt or misclassified. Ceiling
  6.5 m/s = 2:34/km: far above the owner's fastest plausible efforts
  (measured best 20-min ≈ 4:17/km = 3.89 m/s) yet far below the corrupt
  records (7.32-7.87 m/s). It is deliberately an OWNER band: a world-class
  5 km (~7.6 m/s) would be rejected by design, and the owner may widen
  the constant — the rejection always says so in its detail.
- ``ride`` = (1.0, 16.0) m/s. Floor 1.0 m/s = 3.6 km/h: a "ride" whose
  bike barely moved. Ceiling 16.0 m/s = 57.6 km/h: just above the UCI
  hour record (56.792 km/h), so even a world-record hour ride passes as
  an activity average.
- ``swim`` = (0.4, 2.3) m/s. Floor 0.4 m/s = 4:10/100 m: below that an
  activity is bobbing, not swimming. Ceiling 2.3 m/s ≈ peak human sprint
  swim speed; an ACTIVITY AVERAGE of 2.5 m/s (1500 m in 10:00) is
  impossible and is rejected.

Sport mapping: :func:`plausibility_band_key` reuses the engine's own
sport families from :mod:`app.engine.load` (imported, never duplicated,
so the classification cannot drift). ``walk``/``hike`` have NO band: a
walk is never judged with the run band. Unknown sports are
``not_assessable`` with reason ``no_plausibility_band_for_sport``.

Boundary convention: the project's strict one — EXACTLY at a band limit
counts as beyond it (same convention as ``app.engine.intensity``'s pause
rule), so plausibility requires ``floor < avg < ceiling`` strictly.

Per-activity stream cleaning (2)
--------------------------------
:func:`clean_speed_stream` caps implausibly FAST samples of one activity
at::

    cap = min(physiological sample ceiling,
              cap_percentile of the activity's own strictly positive samples)

with ``cap_percentile`` defaulting to :data:`DEFAULT_SPEED_CAP_PERCENTILE`
(0.99) and the ceiling to :data:`RUN_SPEED_SAMPLE_CEILING_MPS` (12.5 m/s
for running — peak human sprinting speed, Usain Bolt's top speed ≈
12.4 m/s; GPS samples above it are spikes by construction). The
percentile part follows the activity's OWN distribution, so a sparse
spike (well under 1% of samples) cannot move the cap: the parent's key
requirement is that a synthetic series with injected spikes yields the
SAME best effort as the clean series, while a genuinely fast SUSTAINED
segment (10% of the samples, far above the 1% tail) is preserved. The
ceiling part backstops activities whose whole tail is corrupt, where the
percentile alone would follow the corruption. Slow samples (zero, rest)
and ``None`` gaps pass through untouched — only implausibly FAST samples
are touched. The 0.99 default trades recall of the corruption (spikes
must stay below 1% of samples) against preservation of real top-end
running; it is an explicit parameter, documented here.

Sample-ceiling provenance: run 12.5 m/s (peak human sprint speed);
ride 30 m/s = 108 km/h (fast descents rarely exceed ~100 km/h); swim
3.0 m/s (safely above sprint-burst swim speed). All owner-reviewable.

Robust run-threshold derivation (3)
-----------------------------------
:func:`derive_run_threshold_candidate` replaces max-of-bests with the
MEDIAN across runs of the per-run best efforts (a few corrupt runs
cannot dominate a median) at the duration ladder
:data:`DEFAULT_THRESHOLD_DURATIONS_S` (3-20 min, the CS fit window). It
then builds the evidence trail:

- robust best efforts per duration (:attr:`RunThresholdCandidate.best_efforts_mps`);
- the CS/D' fit (:func:`app.engine.zones.fit_critical_speed`) over the
  robust curve when at least two in-window points exist;
- the ANCHOR: the CS fit when it exists, otherwise the robust best effort
  at the longest available duration (the classic ~20-min threshold proxy);
- the pace observed at threshold heart rate: the MEDIAN speed of the
  samples whose HR lies strictly inside the threshold HR band (band
  membership follows the project's strict boundary convention — exactly
  at an edge counts as outside); runs without an HR stream simply
  contribute no HR evidence;
- the AGREEMENT between anchor and HR-band pace, defined numerically as
  :data:`DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE` (0.10, 10%) against::

      agreement_relative_deviation = |anchor - hr_pace| / max(anchor, hr_pace)

  the relative gap measured against the LARGER (more optimistic)
  estimate — symmetric in the two statistics, so the rule does not
  depend on which one is anchored, and always ≤ 1 so a tolerance of
  100% admits every positive pair. 10% tolerates normal day-to-day
  variability of threshold pace; the parent's real-data disagreement
  (4.54 vs 3.89 m/s = 16.7% by this rule) exceeds it.

Outcomes (never a fabricated single number):

- ``candidate``: enough usable runs and either no HR evidence (the anchor
  is reported WITHOUT cross-check and ``agreement_relative_deviation`` is
  ``None``) or the deviation is within tolerance. ``threshold_pace_mps``
  is the anchor.
- ``insufficient_data``: no runs, fewer usable runs than ``min_runs``,
  no usable best-effort evidence, or a nonpositive anchor. HR evidence
  ALONE is never a candidate: best-effort evidence is required.
- ``contradictory_data``: the deviation exceeds the tolerance — both
  estimates are reported with their disagreement and
  ``threshold_pace_mps`` stays ``None``.

All result types are frozen dataclasses per the engine convention.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Final, Literal

from app.engine.load import (
    _CYCLING_SPORTS,
    _RUNNING_SPORTS,
    _SWIMMING_SPORTS,
)
from app.engine.zones import (
    CriticalSpeedFit,
    fit_critical_speed,
    mean_maximal_speed_curve,
)

__all__ = [
    "DEFAULT_PLAUSIBILITY_BANDS_MPS",
    "DEFAULT_SPEED_CAP_PERCENTILE",
    "DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE",
    "DEFAULT_THRESHOLD_DURATIONS_S",
    "RUN_SPEED_SAMPLE_CEILING_MPS",
    "SAMPLE_SPEED_CEILINGS_MPS",
    "ActivityPlausibility",
    "CleanedRun",
    "CleanedSpeedStream",
    "RunThresholdCandidate",
    "assess_activity_plausibility",
    "clean_speed_stream",
    "derive_run_threshold_candidate",
    "plausibility_band_key",
]


# ---------------------------------------------------------------------------
# 1. Activity-level plausibility
# ---------------------------------------------------------------------------

PlausibilityBandKey = Literal["run", "ride", "swim"]
"""Stable keys of the sports that carry a plausibility band."""


DEFAULT_PLAUSIBILITY_BANDS_MPS: Final[dict[str, tuple[float, float]]] = {
    "run": (1.5, 6.5),
    "ride": (1.0, 16.0),
    "swim": (0.4, 2.3),
}
"""Average-speed plausibility bands (floor, ceiling) in m/s per sport key.

OWNER-REVIEWABLE constants — see the module docstring for the full
provenance of every bound. The mapping is keyed by
:func:`plausibility_band_key`'s sport families (``run`` / ``ride`` /
``swim``), not by raw activity type, so ``VirtualRun`` and ``Run`` share
the run band and cannot drift apart.
"""


def plausibility_band_key(sport: str) -> PlausibilityBandKey | None:
    """Map a Strava-style activity type onto its plausibility-band key.

    Matched case-insensitively with surrounding whitespace stripped, reusing
    the engine's own sport families from :mod:`app.engine.load` (imported,
    never duplicated). Returns ``None`` for every sport WITHOUT a band —
    including walking sports (a walk is never judged with the run band),
    strength sports and unknown types.
    """
    normalized = sport.strip().lower()
    if normalized in _RUNNING_SPORTS:
        return "run"
    if normalized in _CYCLING_SPORTS:
        return "ride"
    if normalized in _SWIMMING_SPORTS:
        return "swim"
    return None


@dataclass(frozen=True, slots=True)
class ActivityPlausibility:
    """Outcome of the activity-level plausibility assessment.

    ``outcome`` is ``"plausible"``, ``"implausible"`` or
    ``"not_assessable"``; ``reason`` is a machine-readable key (``None``
    exactly when the activity is plausible); ``average_speed_mps`` is the
    distance/duration average when both fields are present and positive
    (``None`` otherwise — a nonpositive pair has no meaningful average);
    ``detail`` is a human-readable explanation carrying the band values.
    """

    outcome: Literal["plausible", "implausible", "not_assessable"]
    reason: str | None
    average_speed_mps: float | None
    detail: str


def assess_activity_plausibility(
    sport: str,
    distance_m: float | None,
    duration_s: float | None,
    *,
    bands: Mapping[str, tuple[float, float]] = DEFAULT_PLAUSIBILITY_BANDS_MPS,
) -> ActivityPlausibility:
    """Judge one activity's plausibility from distance and duration alone.

    The average speed ``distance / duration`` must satisfy the sport's
    band STRICTLY (``floor < avg < ceiling`` — exactly at a limit counts
    as beyond it, the project's strict boundary convention). Reported
    reasons, in evaluation order:

    - ``no_plausibility_band_for_sport`` (``not_assessable``): the sport
      has no band (walking, strength, unknown types).
    - ``missing_distance_or_duration`` (``not_assessable``): a ``None``
      summary field — reported, never guessed.
    - ``nonpositive_distance_or_duration`` (``implausible``): a
      nonpositive field is corrupt data, not a missing one.
    - ``average_speed_above_ceiling`` / ``average_speed_below_floor``
      (``implausible``): the average sits at or beyond a band limit.

    ``bands`` overrides the defaults (owner-reviewable parameters, never
    buried); the result is a frozen :class:`ActivityPlausibility` whose
    ``detail`` always names the band values used.
    """
    band_key = plausibility_band_key(sport)
    if band_key is None or band_key not in bands:
        return ActivityPlausibility(
            outcome="not_assessable",
            reason="no_plausibility_band_for_sport",
            average_speed_mps=None,
            detail=(
                f"sport {sport!r} has no plausibility band "
                f"(known band keys: {sorted(bands)})"
            ),
        )
    floor_mps, ceiling_mps = bands[band_key]
    if distance_m is None or duration_s is None:
        return ActivityPlausibility(
            outcome="not_assessable",
            reason="missing_distance_or_duration",
            average_speed_mps=None,
            detail=(
                f"distance_m={distance_m!r}, duration_s={duration_s!r}: a "
                "missing summary field is reported, never guessed"
            ),
        )
    if distance_m <= 0.0 or duration_s <= 0.0:
        return ActivityPlausibility(
            outcome="implausible",
            reason="nonpositive_distance_or_duration",
            average_speed_mps=None,
            detail=(
                f"distance_m={distance_m!r}, duration_s={duration_s!r}: "
                "nonpositive summary fields are corrupt data"
            ),
        )
    average_speed = distance_m / duration_s
    if average_speed >= ceiling_mps:
        return ActivityPlausibility(
            outcome="implausible",
            reason="average_speed_above_ceiling",
            average_speed_mps=average_speed,
            detail=(
                f"average speed {average_speed:.3f} m/s is at or above the "
                f"{band_key} ceiling {ceiling_mps!r} m/s (strict band: "
                f"{floor_mps!r} < avg < {ceiling_mps!r} m/s)"
            ),
        )
    if average_speed <= floor_mps:
        return ActivityPlausibility(
            outcome="implausible",
            reason="average_speed_below_floor",
            average_speed_mps=average_speed,
            detail=(
                f"average speed {average_speed:.3f} m/s is at or below the "
                f"{band_key} floor {floor_mps!r} m/s (strict band: "
                f"{floor_mps!r} < avg < {ceiling_mps!r} m/s)"
            ),
        )
    return ActivityPlausibility(
        outcome="plausible",
        reason=None,
        average_speed_mps=average_speed,
        detail=(
            f"average speed {average_speed:.3f} m/s is inside the {band_key} "
            f"band ({floor_mps!r} < avg < {ceiling_mps!r} m/s)"
        ),
    )


# ---------------------------------------------------------------------------
# 2. Per-activity stream cleaning
# ---------------------------------------------------------------------------

DEFAULT_SPEED_CAP_PERCENTILE: Final[float] = 0.99
"""Default cap percentile of the activity's own positive speed samples.

The cap follows the activity's own distribution: sparse GPS spikes (well
under 1% of samples) cannot move the 99th percentile, while a genuinely
fast SUSTAINED segment (an order of magnitude more samples) defines it.
Explicit parameter of :func:`clean_speed_stream`; see the module
docstring for the documented trade-off.
"""

RUN_SPEED_SAMPLE_CEILING_MPS: Final[float] = 12.5
"""Absolute per-sample speed ceiling for running: 12.5 m/s.

Peak human sprinting speed (Usain Bolt's top speed ≈ 12.4 m/s); a GPS
speed sample above it is a spike by construction. Backstop for
activities whose whole sample tail is corrupt, where the percentile
alone would follow the corruption. OWNER-REVIEWABLE.
"""

SAMPLE_SPEED_CEILINGS_MPS: Final[dict[str, float]] = {
    "run": RUN_SPEED_SAMPLE_CEILING_MPS,
    "ride": 30.0,
    "swim": 3.0,
}
"""Per-sample physiological ceilings (m/s) per sport key.

OWNER-REVIEWABLE constants — provenance in the module docstring. A sport
without an entry has no ceiling: :func:`clean_speed_stream` refuses
rather than inventing one, unless the caller passes ``ceiling_mps``
explicitly.
"""


@dataclass(frozen=True, slots=True)
class CleanedSpeedStream:
    """Result of :func:`clean_speed_stream`.

    ``samples`` is the cleaned per-second sequence (same length and
    ``None``/nonpositive positions as the input; only implausibly FAST
    samples were capped); ``cap_mps`` the effective cap actually applied
    (``min(ceiling, percentile)``); ``n_samples_capped`` how many samples
    exceeded it; ``n_positive_samples`` how many strictly positive
    samples the input carried (the percentile's population).
    """

    samples: tuple[float | None, ...]
    cap_mps: float
    n_samples_capped: int
    n_positive_samples: int


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile of an ALREADY-SORTED sequence.

    The ``numpy`` "linear" convention: rank ``r = fraction * (n - 1)`` and
    interpolate between the neighbouring order statistics. Defined for
    ``0 <= fraction <= 1`` and non-empty input (both guaranteed by the
    caller).
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] + weight * (
        sorted_values[upper] - sorted_values[lower]
    )


def clean_speed_stream(
    speed_samples: Sequence[float | None],
    *,
    sport: str = "run",
    cap_percentile: float = DEFAULT_SPEED_CAP_PERCENTILE,
    ceiling_mps: float | None = None,
) -> CleanedSpeedStream:
    """Cap implausibly FAST speed samples of one activity's stream.

    Rule (module docstring, section 2)::

        cap = min(ceiling, cap_percentile of the strictly positive samples)

    The percentile is computed over the activity's OWN strictly positive
    samples (raw values, spikes included — sparse spikes stay far below
    the top 1% by definition of "sparse"). ``ceiling_mps`` defaults to the
    sport's documented physiological sample ceiling
    (:data:`SAMPLE_SPEED_CEILINGS_MPS`); an unknown sport without an
    explicit ceiling raises ``ValueError`` instead of inventing one.

    Only implausibly FAST samples are touched: ``None`` gaps, zeros and
    sub-cap samples pass through untouched (rest is rest, not corruption).
    Samples strictly above the cap are replaced BY the cap (capping, not
    dropping, keeps the stream aligned with HR and other streams).

    Validation (``ValueError``, never silent defaults): empty / all-
    ``None`` / no strictly positive sample ("no usable speed data");
    ``cap_percentile`` outside the open interval (0, 1); a nonpositive
    explicit ceiling; an unknown sport without an explicit ceiling.
    """
    if not 0.0 < cap_percentile < 1.0:
        raise ValueError(
            f"cap_percentile must be within the open interval (0, 1), "
            f"got {cap_percentile!r}"
        )
    if ceiling_mps is None:
        normalized_sport = sport.strip().lower()
        if normalized_sport not in SAMPLE_SPEED_CEILINGS_MPS:
            raise ValueError(
                f"no sample speed ceiling defined for sport {sport!r} "
                f"(known sports: {sorted(SAMPLE_SPEED_CEILINGS_MPS)}); pass "
                "ceiling_mps explicitly to clean an unmapped sport"
            )
        ceiling = SAMPLE_SPEED_CEILINGS_MPS[normalized_sport]
    else:
        ceiling = ceiling_mps
    if ceiling <= 0.0:
        raise ValueError(f"ceiling_mps must be positive, got {ceiling!r}")

    samples = list(speed_samples)
    positive = sorted(s for s in samples if s is not None and s > 0.0)
    if not positive:
        raise ValueError(
            "no usable speed data: samples sequence is empty, all missing, "
            "or contains no strictly positive sample"
        )
    cap = min(ceiling, _percentile(positive, cap_percentile))
    cleaned: list[float | None] = []
    n_capped = 0
    for sample in samples:
        if sample is not None and sample > cap:
            cleaned.append(cap)
            n_capped += 1
        else:
            cleaned.append(sample)
    return CleanedSpeedStream(
        samples=tuple(cleaned),
        cap_mps=cap,
        n_samples_capped=n_capped,
        n_positive_samples=len(positive),
    )


# ---------------------------------------------------------------------------
# 3. Robust run-threshold derivation
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLD_DURATIONS_S: Final[tuple[int, ...]] = (180, 300, 480, 600, 900, 1200)
"""Best-effort duration ladder (seconds) for the threshold derivation.

OWNER CHOICE — the 3-20 min window that feeds the CS fit
(:data:`app.engine.zones.CS_MIN_DURATION_S` .. :data:`app.engine.zones.
CS_MAX_DURATION_S`), a subset of the run mean-maximal speed ladder. The
derivation aggregates each duration with the MEDIAN across runs.
"""

DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE: Final[float] = 0.10
"""Default agreement tolerance between anchor and threshold-HR pace.

The two independent estimates may differ by at most 10% (relative to the
larger of the two) for the evidence to count as agreeing. Established
against the parent's real-data finding: the corrupt-statistic disagreement
(4.54 vs 3.89 m/s = 16.7% by this rule) exceeds it, while normal
day-to-day variability of threshold pace stays inside. Explicit,
documented parameter — never magic.
"""

RunThresholdOutcome = Literal["candidate", "insufficient_data", "contradictory_data"]
"""Stable outcome keys of :func:`derive_run_threshold_candidate`."""


@dataclass(frozen=True, slots=True)
class CleanedRun:
    """One activity's cleaned streams, ready for the threshold derivation.

    ``speed_mps`` is the per-second speed sequence (``None`` marks a
    stream gap) — typically the output of :func:`clean_speed_stream`;
    ``hr_bpm`` is the ALIGNED per-second HR sequence or ``None`` when the
    activity has no HR stream. Misaligned streams raise ``ValueError``
    in :func:`derive_run_threshold_candidate` (the ingest parser emits
    aligned streams; misalignment is a bug, not a graceful-degradation
    case).
    """

    speed_mps: Sequence[float | None]
    hr_bpm: Sequence[float | None] | None = None


@dataclass(frozen=True, slots=True)
class RunThresholdCandidate:
    """Typed run-threshold candidate with its full evidence trail.

    ``threshold_pace_mps`` is non-``None`` EXACTLY when ``outcome`` is
    ``"candidate"`` — no pace is ever fabricated from disagreeing
    evidence. ``best_efforts_mps`` maps each duration (seconds) to the
    MEDIAN across runs of the per-run best efforts (the robust statistic
    that replaced max-of-bests); ``cs_fit`` is the CS/D' fit over the
    robust curve when it exists; ``anchor_mps`` is the primary evidence
    (the CS value when the fit exists, otherwise the robust best effort
    at the longest available duration); ``threshold_hr_pace_mps`` is the
    median speed of the in-band HR samples and
    ``threshold_hr_sample_count`` how many samples backed it (0 when
    there is no HR evidence); ``agreement_relative_deviation`` is the
    numeric disagreement (``None`` when there is no HR evidence to
    compare against); ``n_runs_input`` / ``n_runs_used`` count the input
    runs and those that produced usable best-effort evidence; ``detail``
    is a human-readable explanation of the outcome.
    """

    outcome: RunThresholdOutcome
    threshold_pace_mps: float | None
    threshold_pace_sec_per_km: float | None
    best_efforts_mps: dict[int, float] = field(default_factory=dict)
    cs_fit: CriticalSpeedFit | None = None
    anchor_mps: float | None = None
    threshold_hr_pace_mps: float | None = None
    threshold_hr_sample_count: int = 0
    agreement_relative_deviation: float | None = None
    n_runs_input: int = 0
    n_runs_used: int = 0
    detail: str = ""


def _insufficient(
    runs: Sequence[CleanedRun], detail: str
) -> RunThresholdCandidate:
    """Build the ``insufficient_data`` result (no pace is ever invented)."""
    return RunThresholdCandidate(
        outcome="insufficient_data",
        threshold_pace_mps=None,
        threshold_pace_sec_per_km=None,
        n_runs_input=len(runs),
        detail=detail,
    )


def derive_run_threshold_candidate(
    runs: Sequence[CleanedRun],
    *,
    threshold_hr_band_bpm: tuple[float, float],
    durations_s: Sequence[int] = DEFAULT_THRESHOLD_DURATIONS_S,
    min_runs: int = 3,
    agreement_tolerance: float = DEFAULT_THRESHOLD_AGREEMENT_TOLERANCE,
    min_valid_fraction: float = 1.0,
) -> RunThresholdCandidate:
    """Derive the run threshold pace from a history of cleaned runs.

    Robust statistic: for each duration in ``durations_s`` the per-run
    best efforts (:func:`app.engine.zones.mean_maximal_speed_curve`) are
    aggregated with the MEDIAN across runs — the max-of-bests statistic
    this derivation replaced is the most outlier-sensitive one possible,
    and one corrupt run must not dominate. The ANCHOR is the CS/D' fit
    over the robust curve (:func:`app.engine.zones.fit_critical_speed`)
    when it exists, otherwise the robust best effort at the longest
    available duration.

    The threshold-HR pace is the median speed of the samples whose HR
    lies STRICTLY inside ``threshold_hr_band_bpm`` (exactly at an edge
    counts as outside — the project's strict boundary convention), pooled
    across all runs that carry an HR stream; runs without one contribute
    no HR evidence but still contribute best efforts.

    Agreement rule (numeric, documented):
    ``|anchor - hr_pace| / max(anchor, hr_pace)`` must be within
    ``agreement_tolerance`` (default 0.10, 10%).

    Outcomes: ``candidate`` (enough usable runs, and either no HR
    evidence — the anchor is reported WITHOUT cross-check — or agreement
    within tolerance; ``threshold_pace_mps`` is the anchor),
    ``insufficient_data`` (no runs / fewer usable runs than ``min_runs``
    / no usable best-effort evidence / nonpositive anchor — HR evidence
    alone is never a candidate) or ``contradictory_data`` (deviation
    beyond tolerance; both estimates are reported, no pace is fabricated).

    Validation (``ValueError``, never silent): nonpositive ``min_runs``
    or ``agreement_tolerance``, a non-ascending HR band, an empty
    ``durations_s``, and HR streams whose length differs from their
    speed stream (``"must align"``).

    ``min_valid_fraction`` flows into the per-run best-effort extraction
    (default strict 1.0, the engine convention; callers may relax it for
    gap-heavy streams explicitly).
    """
    if min_runs <= 0:
        raise ValueError(f"min_runs must be positive, got {min_runs!r}")
    if agreement_tolerance <= 0.0:
        raise ValueError(
            f"agreement_tolerance must be positive, got {agreement_tolerance!r}"
        )
    band_floor, band_ceiling = threshold_hr_band_bpm
    if not band_floor < band_ceiling:
        raise ValueError(
            f"threshold HR band must be ascending (floor < ceiling), got "
            f"{threshold_hr_band_bpm!r}"
        )
    durations = tuple(durations_s)
    if not durations:
        raise ValueError(
            "durations_s must not be empty: no best-effort duration was requested"
        )
    for run in runs:
        if run.hr_bpm is not None and len(run.hr_bpm) != len(run.speed_mps):
            raise ValueError(
                "HR and speed streams must align (same length) for every run: "
                f"{len(run.speed_mps)} != {len(run.hr_bpm)}"
            )

    if not runs:
        return _insufficient(runs, "no runs provided: the history is empty")

    # Per-run best-effort curves; unusable streams are REPORTED via
    # n_runs_used, never silently mixed into the aggregate.
    per_run_curves: list[dict[int, float]] = []
    for run in runs:
        try:
            curve = mean_maximal_speed_curve(
                run.speed_mps,
                durations_s=durations,
                min_valid_fraction=min_valid_fraction,
            )
        except ValueError:
            continue  # all-``None`` stream: contributes no best effort
        if curve:
            per_run_curves.append(curve)
    n_used = len(per_run_curves)
    if n_used < min_runs:
        return _insufficient(
            runs,
            f"only {n_used} of {len(runs)} runs produced usable best-effort "
            f"evidence; min_runs={min_runs} required",
        )

    # Robust (median-across-runs) curve per duration.
    best_efforts: dict[int, float] = {}
    for duration in durations:
        values = [curve[duration] for curve in per_run_curves if duration in curve]
        if values:
            best_efforts[duration] = median(values)

    # Anchor: the CS fit when it exists, else the robust best effort at
    # the longest available duration.
    cs_fit_result: CriticalSpeedFit | None
    try:
        cs_fit_result = fit_critical_speed(best_efforts)
    except ValueError:
        cs_fit_result = None
    anchor: float | None = None
    if cs_fit_result is not None:
        anchor = cs_fit_result.cs_mps
    elif best_efforts:
        anchor = best_efforts[max(best_efforts)]
    if anchor is None or anchor <= 0.0:
        return _insufficient(
            runs,
            "no usable best-effort evidence: no run produced a positive "
            f"best effort at the requested durations {list(durations)}",
        )

    # Threshold-HR evidence: median speed of strictly in-band samples,
    # pooled across every run that carries an HR stream.
    in_band_speeds: list[float] = []
    for run in runs:
        if run.hr_bpm is None:
            continue  # no HR stream: contributes no HR evidence
        for speed, hr in zip(run.speed_mps, run.hr_bpm, strict=True):
            if hr is not None and band_floor < hr < band_ceiling and speed is not None:
                in_band_speeds.append(speed)
    hr_pace: float | None = median(in_band_speeds) if in_band_speeds else None

    if hr_pace is None:
        return RunThresholdCandidate(
            outcome="candidate",
            threshold_pace_mps=anchor,
            threshold_pace_sec_per_km=1000.0 / anchor,
            best_efforts_mps=best_efforts,
            cs_fit=cs_fit_result,
            anchor_mps=anchor,
            threshold_hr_pace_mps=None,
            threshold_hr_sample_count=0,
            agreement_relative_deviation=None,
            n_runs_input=len(runs),
            n_runs_used=n_used,
            detail=(
                f"anchor {anchor:.3f} m/s reported WITHOUT cross-check: no "
                f"sample lies strictly inside the threshold HR band "
                f"{threshold_hr_band_bpm!r} bpm (or no run carries an HR stream)"
            ),
        )

    deviation = abs(anchor - hr_pace) / max(anchor, hr_pace)
    if deviation > agreement_tolerance:
        relation = "faster" if hr_pace > anchor else "slower"
        return RunThresholdCandidate(
            outcome="contradictory_data",
            threshold_pace_mps=None,
            threshold_pace_sec_per_km=None,
            best_efforts_mps=best_efforts,
            cs_fit=cs_fit_result,
            anchor_mps=anchor,
            threshold_hr_pace_mps=hr_pace,
            threshold_hr_sample_count=len(in_band_speeds),
            agreement_relative_deviation=deviation,
            n_runs_input=len(runs),
            n_runs_used=n_used,
            detail=(
                f"evidence disagrees: the threshold-HR pace {hr_pace:.3f} m/s "
                f"is {relation} than the robust best-effort anchor "
                f"{anchor:.3f} m/s (relative deviation {deviation:.3f} > "
                f"tolerance {agreement_tolerance!r}); refusing to fabricate "
                "a single pace from disagreeing evidence"
            ),
        )
    return RunThresholdCandidate(
        outcome="candidate",
        threshold_pace_mps=anchor,
        threshold_pace_sec_per_km=1000.0 / anchor,
        best_efforts_mps=best_efforts,
        cs_fit=cs_fit_result,
        anchor_mps=anchor,
        threshold_hr_pace_mps=hr_pace,
        threshold_hr_sample_count=len(in_band_speeds),
        agreement_relative_deviation=deviation,
        n_runs_input=len(runs),
        n_runs_used=n_used,
        detail=(
            f"anchor {anchor:.3f} m/s and threshold-HR pace {hr_pace:.3f} m/s "
            f"agree within tolerance (relative deviation {deviation:.3f} <= "
            f"{agreement_tolerance!r})"
        ),
    )

