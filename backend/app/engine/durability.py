"""Durability: Efficiency Factor, aerobic decoupling (Pa:HR) and long-session
durability trends (RID-8/RID-9, PROJECT_BRIEF section 7.6).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core`` (PROJECT_BRIEF sections 6 and 14). Sessions come in as explicit
per-second streams (the ingest-parser convention, ``None`` marks a gap) and
as frozen :class:`DurabilitySessionPoint` values; every threshold, the
reference band, the steadiness limit and the "long session" definition are
parameters. Side effects, persistence and settings reads belong in callers
(mirrored into Settings with source comments in RID-10, like the
LOAD-11/ZON-11 patterns).

Efficiency Factor (RID-8, section 7.6)
--------------------------------------
The Efficiency Factor (EF) divides a normalised intensity measure by the
athlete's average heart rate for the session (Friel, "The Triathlete's
Training Bible"; section 7.6):

    bike : EF = NP  / avg HR     (watts per bpm;  NP  per
                                  :func:`app.engine.load.normalized_power`)
    run  : EF = NGS / avg HR     ((m/s) per bpm; NGS per
                                  :func:`app.engine.load.normalized_graded_speed`)

NP and NGS are REUSED from :mod:`app.engine.load` — never reimplemented
here — so the EF inherits their exact gap and window semantics. The HR
numerator's counterpart is the athlete's AVERAGE heart rate for the session
(or for the session half, in the decoupling below): EF is a coarse
aerobic-economy proxy in units of W/bpm (bike) or (m/s)/bpm (run); it is
only comparable WITHIN a sport and within a stable training phase, never
across sports. A non-positive average HR or a non-positive normalised value
raises ``ValueError`` — a misconfigured or dead sensor must never produce a
fabricated EF.

Aerobic decoupling, Pa:HR (RID-8, section 7.6)
----------------------------------------------
The session is split into its FIRST half and SECOND half of per-second
samples, EF is computed for each half exactly as above (per-half NP or NGS
over the per-half average HR), and the decoupling is:

    Pa:HR = (EF_first_half - EF_second_half) / EF_first_half

SIGN CONVENTION (explicit, because Pa:HR is published both ways): a
POSITIVE value means EF DETERIORATED in the second half — the classic
aerobic decoupling (heart rate drifting up, or power fading, at constant
effort). A NEGATIVE value means EF IMPROVED in the second half (a negative
split or falling HR at constant output). Zero means the halves were equally
efficient.

Split rule: with ``n`` samples, the FIRST half receives the extra sample
when ``n`` is odd — the first half is ``samples[:(n + 1) // 2]``, the
second half the remainder. The first half is chosen because decoupling is
quoted relative to the FIRST half (the formula's denominator), so the
reference quantity keeps the larger share; the reported ``n_first_half`` /
``n_second_half`` fields expose the split. Both halves must contain at
least one sample; an HR stream gap (``None``) is excluded from its half's
mean, and a half with no usable HR sample raises ``ValueError``.

Reference band: Friel's published rule of thumb — a Pa:HR BELOW 5% on long
steady sessions suggests good aerobic durability (LITERATURE reference;
section 7.6 fixes the 5%). The band is DESCRIPTIVE: this module reports
whether the value sits within it (``within_reference_band``) and never
prescribes. Boundary convention: the band is STRICT (ZON-9 / readiness
precedents) — a value exactly AT 5% is reported as NOT within the band,
and the comparison runs through ``_same_boundary`` (the same tolerant rule
as the zone tables) so float dust at the boundary cannot flip the outcome.

Steadiness guard (documented, configurable)
-------------------------------------------
Decoupling is meaningful only on STEADY sessions: a rider who surges in the
second half produces a large Pa:HR that says nothing about aerobic
durability. The module therefore implements a configurable steadiness
guard (rather than leaving steadiness to the caller): the intensity
measures of the two halves (NP per half for bike, NGS per half for run —
the same quantities the EFs are built from) must agree within
``max_intensity_drift`` (default :data:`DEFAULT_MAX_HALF_INTENSITY_DRIFT`
= 15%, OWNER CHOICE — loose enough for normal cardiac drift on a steady
ride, tight enough to exclude intervals/surges). ``drift =
|intensity_second_half - intensity_first_half| / intensity_first_half``;
the guard is symmetric in sign (a harder second half trips it equally) and
rejects only when drift is STRICTLY beyond the limit (tolerant boundary,
the engine's standard convention). A rejected session returns
``status="not_steady"`` with the measured drift as evidence and NO
decoupling value — the module refuses to publish a Pa:HR its own guard
calls uninterpretable. Callers who prefer to decide steadiness themselves
can set ``max_intensity_drift`` very high (the guard then never fires).

Durability trends on long sessions (RID-9, section 7.6; Maunder et al. 2021)
-----------------------------------------------------------------------------
"Durability" — the ability to resist deterioration of the work-rate/HR
relationship over prolonged exercise — is studied on PROLONGED sessions
(Maunder et al. 2021, "durability" as a determinant of endurance
performance; PROJECT_BRIEF section 7.6 seed list). The trend
(:func:`durability_trend`) tracks the decoupling of the athlete's LONG
sessions inside a trailing window:

- "Long" is ``min_long_session_seconds`` (default
  :data:`DEFAULT_MIN_LONG_SESSION_SECONDS` = 90 min, OWNER CHOICE with the
  rationale below). SHORT sessions are EXCLUDED (reported as ineligible,
  reason ``"below_min_duration"``): their decoupling is dominated by
  warm-up transients, interval structure and pacing choices rather than by
  aerobic durability, so including them would measure session design, not
  the durability trait Maunder et al. describe — and would swamp the
  long-session signal with noisier, systematically different values.
  90 minutes is a conservative lower bound for the "prolonged" domain in
  which durability effects (cardiac drift, EF decline at fixed output)
  materialise, and matches the owner's real long-ride/long-run profile.
- Window: the trailing ``window_days`` (default
  :data:`DEFAULT_TREND_WINDOW_DAYS` = 84 days = 12 weeks, OWNER CHOICE —
  long enough to hold several long sessions around the owner's weekly
  long ride, short enough to reflect the current build). The window
  anchors on the LATEST session date and covers
  ``[window_end - (window_days - 1) days, window_end]``; older sessions
  are ineligible (reason ``"outside_window"``).
- Trend statistic: the ordinary-least-squares SLOPE of Pa:HR against
  session date (in decoupling fraction per day) over the ELIGIBLE
  sessions — the same closed-form OLS the CP/CS fits use. The eligible
  sessions (date, duration, decoupling), the session counts and the window
  are all reported alongside. Direction is descriptive and sign-based:
  slope < 0 = ``"improving"`` (Pa:HR falling over time = the athlete holds
  EF longer), slope > 0 = ``"worsening"``, slope == 0 (tolerantly — float
  dust at zero is still stable) = ``"stable"``.
- Insufficient data: sessions below the minimum duration are ineligible
  WITH a reason; a session with no decoupling value (not computable
  upstream) is ineligible (reason ``"no_decoupling_value"``); fewer
  eligible sessions than ``min_sessions`` (default
  :data:`DEFAULT_MIN_TREND_SESSIONS` = 3, OWNER CHOICE — two points define
  a line trivially and cannot be distinguished from noise, so a trend
  needs at least three) yields an explicit ``status="insufficient_data"``
  outcome with the counts. The function NEVER computes a trend from one
  point and NEVER fabricates a zero.

Purity: all functions are pure and fully typed; validation failures raise
``ValueError`` — the engine never silently drops or fabricates data.

Configurable constants (RID-8/RID-9, §7/§14)
--------------------------------------------
Every constant below is a function parameter whose default is the
documented module constant (the documented fallback). This module never
imports settings (§6 purity); mirroring into ``app.core.settings`` with
source comments is RID-10's scope:

- Friel decoupling reference band 5% (``reference_band``): LITERATURE
  (section 7.6: "< 5% on long steady sessions suggests good aerobic
  durability (Friel)").
- Steadiness guard limit 15% half-intensity drift
  (``max_intensity_drift``): OWNER CHOICE — no source publishes a
  steadiness threshold; 15% tolerates normal drift on a steady long ride
  while excluding interval-style swings (see above).
- "Long session" threshold 90 min (``min_long_session_seconds``):
  OWNER CHOICE — rationale above (prolonged-exercise domain; short
  sessions measure pacing, not durability).
- Trend window 84 days = 12 weeks (``window_days``): OWNER CHOICE —
  roughly one training build; long enough for several long sessions,
  short enough to stay current.
- Minimum eligible sessions 3 (``min_sessions``): OWNER CHOICE — two
  points cannot be distinguished from noise.
"""

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from app.engine.load import NP_WINDOW_SAMPLES, normalized_graded_speed, normalized_power

__all__ = [
    "DEFAULT_DECOUPLING_REFERENCE_BAND",
    "DEFAULT_MAX_HALF_INTENSITY_DRIFT",
    "DEFAULT_MIN_LONG_SESSION_SECONDS",
    "DEFAULT_MIN_TREND_SESSIONS",
    "DEFAULT_TREND_WINDOW_DAYS",
    "DURABILITY_SPORT_KEYS",
    "DurabilitySessionPoint",
    "DurabilitySportKey",
    "DurabilityTrend",
    "IneligibleReason",
    "IneligibleSession",
    "SessionDecoupling",
    "SessionDecouplingStatus",
    "TrendDirection",
    "TrendStatus",
    "aerobic_decoupling",
    "bike_efficiency_factor",
    "durability_trend",
    "efficiency_factor",
    "run_efficiency_factor",
    "session_decoupling",
]


# ---------------------------------------------------------------------------
# Configurable constants (RID-8/RID-9, §7/§14) — provenance in the module
# docstring; every entry is LITERATURE-cited or an explicit OWNER CHOICE.
# ---------------------------------------------------------------------------

DEFAULT_DECOUPLING_REFERENCE_BAND: Final[float] = 0.05
"""Friel decoupling reference band: Pa:HR strictly below 5% on long steady
sessions suggests good aerobic durability. LITERATURE (section 7.6)."""

DEFAULT_MAX_HALF_INTENSITY_DRIFT: Final[float] = 0.15
"""Steadiness-guard limit: the halves' intensity measures (NP or NGS) must
agree within 15% relative drift. OWNER CHOICE (no published threshold)."""

DEFAULT_MIN_LONG_SESSION_SECONDS: Final[float] = 5400.0
""""Long" session threshold for durability trends: 90 minutes.
OWNER CHOICE — rationale in the module docstring (RID-9)."""

DEFAULT_TREND_WINDOW_DAYS: Final[int] = 84
"""Trailing trend window in days (84 = 12 weeks). OWNER CHOICE (RID-9)."""

DEFAULT_MIN_TREND_SESSIONS: Final[int] = 3
"""Minimum eligible long sessions before a trend is computed.
OWNER CHOICE (RID-9): two points cannot be distinguished from noise."""

_SAME_BOUNDARY_REL_TOL: Final[float] = 1e-12
_SAME_BOUNDARY_ABS_TOL: Final[float] = 1e-9


def _same_boundary(a: float, b: float) -> bool:
    """Whether two values are the same boundary under float noise.

    The same tolerant-comparison rule as the zone tables
    (:func:`app.engine.zones._same_boundary`): a caller who recomputes a
    boundary (or a hand-calculated 5% that lands one ULP away) must land
    on the same side. Tolerances stay far tighter than any real
    decoupling difference (1e-9 absolute).
    """
    return math.isclose(a, b, rel_tol=_SAME_BOUNDARY_REL_TOL,
                        abs_tol=_SAME_BOUNDARY_ABS_TOL)


# ---------------------------------------------------------------------------
# Stable keys and vocabularies
# ---------------------------------------------------------------------------

DurabilitySportKey = Literal["bike", "run"]
"""Sports the section 7.6 durability metrics apply to: bike (EF = NP / avg
HR) and run (EF = NGS / avg HR). Swim has no published Pa:HR convention in
the brief; extend this Literal explicitly if one is ever defined."""

DURABILITY_SPORT_KEYS: Final[tuple[DurabilitySportKey, ...]] = ("bike", "run")
"""All durability sport keys (the stable, validated sport namespace)."""

SessionDecouplingStatus = Literal["ok", "not_steady"]
"""Session decoupling status: ``"ok"`` when the steadiness guard passed and
the decoupling is reported; ``"not_steady"`` when the guard rejected the
session (decoupling is then ``None`` — never published degraded)."""

TrendStatus = Literal["trend", "insufficient_data"]
"""Trend outcome status: ``"trend"`` when a slope was computed from at
least ``min_sessions`` eligible sessions; ``"insufficient_data"`` when not
(counts reported, slope ``None`` — never a trend from one point, never a
fabricated zero)."""

TrendDirection = Literal["improving", "worsening", "stable"]
"""Descriptive, sign-based direction of the decoupling slope: ``"improving"``
(Pa:HR falling over time), ``"worsening"`` (rising), ``"stable"``
(zero slope). Describes the OBSERVED series, never advice."""

IneligibleReason = Literal[
    "below_min_duration", "outside_window", "no_decoupling_value"
]
"""Why a session was excluded from the trend: below the "long" threshold,
outside the trailing window, or carrying no decoupling value."""


# ---------------------------------------------------------------------------
# RID-8: Efficiency Factor
# ---------------------------------------------------------------------------


def efficiency_factor(normalized_value: float, avg_hr_bpm: float) -> float:
    """Efficiency Factor EF = normalized intensity / average HR (section 7.6).

    ``normalized_value`` is the engine's Normalized Power in watts for bike
    sessions (units: W per bpm) or the Normalized Graded Speed in m/s for
    run sessions (units: (m/s) per bpm) — reuse
    :func:`app.engine.load.normalized_power` /
    :func:`app.engine.load.normalized_graded_speed` upstream; this function
    never recomputes them. ``avg_hr_bpm`` is the athlete's AVERAGE heart
    rate for the session (bpm).

    Validation: a non-positive average HR or a non-positive normalised
    value raises ``ValueError`` — a dead HR strap or a misconfigured
    threshold must never produce a fabricated EF. EF is only comparable
    within one sport and within a stable training phase (different sports
    have different units).

    Reference: Friel, "The Triathlete's Training Bible" (EF convention);
    section 7.6 (bike NP / avg HR; run NGS / avg HR).
    """
    if not math.isfinite(avg_hr_bpm) or avg_hr_bpm <= 0.0:
        raise ValueError(
            f"average HR must be positive, got {avg_hr_bpm!r} bpm"
        )
    if not math.isfinite(normalized_value) or normalized_value <= 0.0:
        raise ValueError(
            f"normalized value (NP or NGS) must be positive, got "
            f"{normalized_value!r}"
        )
    return normalized_value / avg_hr_bpm


def _mean_hr(hrm_samples: Sequence[float | None], *, half: str) -> float:
    """Mean of the valid (non-``None``) HR samples of one (half-)session.

    HR stream gaps (``None``) are excluded from the mean — the same
    documented convention as the speed/power gap handling in
    :mod:`app.engine.load`. A half with no usable sample raises
    ``ValueError``.
    """
    valid = [s for s in hrm_samples if s is not None]
    if not valid:
        raise ValueError(
            f"no usable HR data in the {half} half: samples are empty or "
            "all missing"
        )
    return math.fsum(valid) / len(valid)


def _checked_streams(
    intensity_samples: Sequence[float | None],
    hr_samples: Sequence[float | None],
) -> tuple[list[float | None], list[float | None]]:
    """Validate a paired per-second intensity/HR stream pair (RID-8).

    Both streams must be non-empty and the same length (aligned per-second,
    the ingest-parser convention); misalignment is a bug, not a graceful
    degradation case (mirrors :func:`app.engine.load.normalized_graded_speed`).
    """
    intensity = list(intensity_samples)
    hr = list(hr_samples)
    if not intensity or not hr:
        raise ValueError(
            "intensity and HR streams must be non-empty: got "
            f"{len(intensity)} and {len(hr)} samples"
        )
    if len(intensity) != len(hr):
        raise ValueError(
            "intensity and HR streams must have the same length: "
            f"{len(intensity)} != {len(hr)}"
        )
    return intensity, hr


def _split_half(
    samples: list[float | None],
) -> tuple[list[float | None], list[float | None]]:
    """Split a stream into its first and second half (documented rule).

    The FIRST half receives the extra sample when the count is odd: with
    ``n`` samples the first half is ``samples[:(n + 1) // 2]``. Rationale
    in the module docstring (the decoupling denominator is the first half).
    """
    mid = (len(samples) + 1) // 2
    return samples[:mid], samples[mid:]


def bike_efficiency_factor(
    power_samples: Sequence[float | None],
    hr_samples: Sequence[float | None],
    *,
    window_samples: int = NP_WINDOW_SAMPLES,
    min_valid_fraction: float = 1.0,
) -> float:
    """Whole-session bike Efficiency Factor EF = NP / avg HR (RID-8).

    Reuses :func:`app.engine.load.normalized_power` (gap and short-file
    semantics documented there; ``window_samples`` / ``min_valid_fraction``
    pass through) and the mean of the valid HR samples. Streams must be
    aligned per-second (same length); a non-positive NP or average HR
    raises ``ValueError``.

    Reference: Friel (EF convention); section 7.6.
    """
    power, hr = _checked_streams(power_samples, hr_samples)
    np_value = normalized_power(
        power, window_samples=window_samples, min_valid_fraction=min_valid_fraction
    )
    return efficiency_factor(np_value, _mean_hr(hr, half="session"))


def run_efficiency_factor(
    speed_samples: Sequence[float | None],
    hr_samples: Sequence[float | None],
    *,
    distance_samples: Sequence[float | None] | None = None,
    altitude_samples: Sequence[float | None] | None = None,
) -> float:
    """Whole-session run Efficiency Factor EF = NGS / avg HR (RID-8).

    Reuses :func:`app.engine.load.normalized_graded_speed` (grade and gap
    semantics documented there; the altitude/distance streams pass through)
    and the mean of the valid HR samples. Streams must be aligned
    per-second; a non-positive NGS or average HR raises ``ValueError``.

    Reference: Friel (EF convention); section 7.6.
    """
    speed, hr = _checked_streams(speed_samples, hr_samples)
    ngs = normalized_graded_speed(
        speed,
        distance_samples=distance_samples,
        altitude_samples=altitude_samples,
    )
    return efficiency_factor(ngs, _mean_hr(hr, half="session"))


# ---------------------------------------------------------------------------
# RID-8: aerobic decoupling (Pa:HR)
# ---------------------------------------------------------------------------


def aerobic_decoupling(ef_first_half: float, ef_second_half: float) -> float:
    """Aerobic decoupling (Pa:HR) of two half-session EFs (RID-8).

    Formula::

        Pa:HR = (EF_first_half - EF_second_half) / EF_first_half

    SIGN CONVENTION (explicit): a POSITIVE value means EF DETERIORATED in
    the second half (the classic aerobic decoupling); a NEGATIVE value
    means EF IMPROVED; zero means the halves were equally efficient.

    Validation: a non-positive EF on either side raises ``ValueError``.

    Reference: Friel, "The Triathlete's Training Bible" (Pa:HR); section 7.6.
    """
    if not math.isfinite(ef_first_half) or ef_first_half <= 0.0:
        raise ValueError(f"first-half EF must be positive, got {ef_first_half!r}")
    if not math.isfinite(ef_second_half) or ef_second_half <= 0.0:
        raise ValueError(f"second-half EF must be positive, got {ef_second_half!r}")
    return (ef_first_half - ef_second_half) / ef_first_half


@dataclass(frozen=True, slots=True)
class SessionDecoupling:
    """Aerobic decoupling of one session with its evidence (RID-8).

    ``status`` is ``"ok"`` when the steadiness guard passed — ``decoupling``
    (fraction; ``decoupling_pct`` = x 100) and the half EFs are populated,
    and ``within_reference_band`` states whether the value sits strictly
    below the Friel 5% reference band (:data:`DEFAULT_DECOUPLING_REFERENCE_BAND`;
    STRICT boundary, tolerant comparison — exactly 5% is NOT within). With
    ``status == "not_steady"`` the guard rejected the session: the EF and
    decoupling fields are ``None`` and the measured ``intensity_drift``
    plus ``detail`` carry the evidence.

    ``ef_first_half`` / ``ef_second_half`` are in the sport's EF units
    (bike W/bpm, run (m/s)/bpm); ``intensity_first_half`` /
    ``intensity_second_half`` are the underlying NP/NGS half values the
    guard compared. ``n_first_half`` / ``n_second_half`` expose the
    documented split rule (the first half holds the extra sample when the
    count is odd). Frozen and slotted.
    """

    sport: DurabilitySportKey
    status: SessionDecouplingStatus
    ef_first_half: float | None
    ef_second_half: float | None
    decoupling: float | None
    decoupling_pct: float | None
    within_reference_band: bool | None
    reference_band: float
    intensity_first_half: float | None
    intensity_second_half: float | None
    intensity_drift: float
    max_intensity_drift: float
    n_samples: int
    n_first_half: int
    n_second_half: int
    detail: str


def session_decoupling(
    sport: DurabilitySportKey,
    intensity_samples: Sequence[float | None],
    hr_samples: Sequence[float | None],
    *,
    distance_samples: Sequence[float | None] | None = None,
    altitude_samples: Sequence[float | None] | None = None,
    max_intensity_drift: float = DEFAULT_MAX_HALF_INTENSITY_DRIFT,
    reference_band: float = DEFAULT_DECOUPLING_REFERENCE_BAND,
    np_window_samples: int = NP_WINDOW_SAMPLES,
    np_min_valid_fraction: float = 1.0,
) -> SessionDecoupling:
    """Aerobic decoupling (Pa:HR) of one bike or run session (RID-8).

    Splits the aligned per-second intensity and HR streams into the two
    documented halves (first half gets the extra sample when odd), computes
    each half's EF (bike: :func:`app.engine.load.normalized_power` of the
    half over the half's mean HR; run:
    :func:`app.engine.load.normalized_graded_speed` of the half), and
    returns the :func:`aerobic_decoupling` result with its evidence.

    STEADINESS GUARD: the halves' intensity measures (NP or NGS) must agree
    within ``max_intensity_drift`` (``drift = |i2 - i1| / i1``, symmetric in
    sign; the guard rejects only STRICTLY beyond the limit, tolerant
    boundary). A rejected session returns ``status="not_steady"`` with no
    decoupling value — steadiness is enforced here, not left to the caller
    (callers who prefer their own rule may raise the limit).

    REFERENCE BAND: ``within_reference_band`` reports whether the
    decoupling sits strictly below ``reference_band`` (default the Friel
    5%); the band is descriptive only — this module reports, it does not
    prescribe.

    Validation: unknown sport, misaligned or empty streams, a session too
    short to split, a half without usable HR data, a non-positive drift
    limit, or a reference band outside ``(0, 1]`` raise ``ValueError``.

    Reference: Friel (Pa:HR, < 5% on long steady sessions); section 7.6.
    """
    if sport not in DURABILITY_SPORT_KEYS:
        raise ValueError(
            f"unknown durability sport {sport!r}; expected one of "
            f"{DURABILITY_SPORT_KEYS}"
        )
    if not math.isfinite(max_intensity_drift) or max_intensity_drift < 0.0:
        raise ValueError(
            "max_intensity_drift must be finite and non-negative, got "
            f"{max_intensity_drift!r}"
        )
    if not math.isfinite(reference_band) or not 0.0 < reference_band <= 1.0:
        raise ValueError(
            f"reference_band must be within (0, 1], got {reference_band!r}"
        )
    intensity, hr = _checked_streams(intensity_samples, hr_samples)
    if len(intensity) < 2:
        raise ValueError(
            "session too short to split into a first and a second half: "
            f"need at least 2 samples, got {len(intensity)}"
        )
    first_int, second_int = _split_half(intensity)
    first_hr, second_hr = _split_half(hr)
    n_first, n_second = len(first_int), len(second_int)

    if sport == "bike":
        intensity_first = normalized_power(
            first_int,
            window_samples=np_window_samples,
            min_valid_fraction=np_min_valid_fraction,
        )
        intensity_second = normalized_power(
            second_int,
            window_samples=np_window_samples,
            min_valid_fraction=np_min_valid_fraction,
        )
    else:
        intensity_first = normalized_graded_speed(
            first_int,
            distance_samples=None
            if distance_samples is None
            else list(distance_samples)[:n_first],
            altitude_samples=None
            if altitude_samples is None
            else list(altitude_samples)[:n_first],
        )
        intensity_second = normalized_graded_speed(
            second_int,
            distance_samples=None
            if distance_samples is None
            else list(distance_samples)[n_first:],
            altitude_samples=None
            if altitude_samples is None
            else list(altitude_samples)[n_first:],
        )
    drift = abs(intensity_second - intensity_first) / intensity_first

    split = f"{n_first}/{n_second} samples"
    if drift > max_intensity_drift and not _same_boundary(
        drift, max_intensity_drift
    ):
        return SessionDecoupling(
            sport=sport,
            status="not_steady",
            ef_first_half=None,
            ef_second_half=None,
            decoupling=None,
            decoupling_pct=None,
            within_reference_band=None,
            reference_band=reference_band,
            intensity_first_half=intensity_first,
            intensity_second_half=intensity_second,
            intensity_drift=drift,
            max_intensity_drift=max_intensity_drift,
            n_samples=len(intensity),
            n_first_half=n_first,
            n_second_half=n_second,
            detail=(
                f"Session rejected as not steady ({split}): the halves' "
                f"intensity measures drift {drift:.1%} apart "
                f"({intensity_first:.4g} vs {intensity_second:.4g}), beyond "
                f"the {max_intensity_drift:.1%} limit, so the decoupling "
                "would not be interpretable on this session. Relax "
                "max_intensity_drift explicitly to override the guard."
            ),
        )

    ef_first = efficiency_factor(intensity_first, _mean_hr(first_hr, half="first"))
    ef_second = efficiency_factor(
        intensity_second, _mean_hr(second_hr, half="second")
    )
    decoupling = aerobic_decoupling(ef_first, ef_second)
    within_band = decoupling < reference_band and not _same_boundary(
        decoupling, reference_band
    )
    band_text = (
        "within the Friel reference band (< 5% decoupling on long steady "
        "sessions suggests good aerobic durability)"
        if within_band
        else "not within the Friel reference band (< 5% decoupling on long "
        "steady sessions suggests good aerobic durability; the boundary is "
        "strict — exactly 5% counts as beyond)"
    )
    return SessionDecoupling(
        sport=sport,
        status="ok",
        ef_first_half=ef_first,
        ef_second_half=ef_second,
        decoupling=decoupling,
        decoupling_pct=decoupling * 100.0,
        within_reference_band=within_band,
        reference_band=reference_band,
        intensity_first_half=intensity_first,
        intensity_second_half=intensity_second,
        intensity_drift=drift,
        max_intensity_drift=max_intensity_drift,
        n_samples=len(intensity),
        n_first_half=n_first,
        n_second_half=n_second,
        detail=(
            f"Pa:HR {decoupling * 100.0:.2f}% over halves {split} "
            f"(EF {ef_first:.4g} -> {ef_second:.4g}, halves' intensity "
            f"drift {drift:.1%}): positive means EF deteriorated in the "
            "second half, negative means it improved. The value is "
            f"{band_text}. Descriptive only; steadiness guard limit "
            f"{max_intensity_drift:.1%}."
        ),
    )


# ---------------------------------------------------------------------------
# RID-9: durability trends on long sessions (Maunder et al. 2021)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DurabilitySessionPoint:
    """One session as a durability-trend input (RID-9).

    ``session_date`` is the athlete's local session date (the as-stored
    date, never timezone-shifted); ``duration_s`` the wall-clock duration;
    ``decoupling`` the session's Pa:HR fraction as computed by
    :func:`session_decoupling` upstream (``None`` when it was not
    computable — reported as ineligible, never substituted). Frozen and
    slotted.
    """

    session_date: date
    duration_s: float
    decoupling: float | None


@dataclass(frozen=True, slots=True)
class IneligibleSession:
    """One session excluded from the durability trend, with its reason
    (RID-9 insufficient-data handling). Frozen and slotted."""

    session_date: date
    duration_s: float
    reason: IneligibleReason
    detail: str


@dataclass(frozen=True, slots=True)
class DurabilityTrend:
    """Durability trend over the athlete's long sessions (RID-9).

    ``status == "trend"`` carries the OLS slope of Pa:HR over session date
    (``slope_per_day``, decoupling fraction per day; negative = Pa:HR
    falling = durability improving) and the descriptive sign-based
    ``direction``. ``status == "insufficient_data"`` reports the counts
    (``n_eligible`` / ``n_ineligible`` / ``min_sessions``) with
    ``slope_per_day`` and ``direction`` as ``None`` — never a trend from
    one point, never a fabricated zero. The eligible sessions, the
    ineligible ones with reasons, and the window bounds are always
    reported for audit. Frozen and slotted.
    """

    status: TrendStatus
    slope_per_day: float | None
    direction: TrendDirection | None
    n_eligible: int
    n_ineligible: int
    min_long_session_seconds: float
    min_sessions: int
    window_days: int
    window_start: date
    window_end: date
    eligible: tuple[DurabilitySessionPoint, ...]
    ineligible: tuple[IneligibleSession, ...]
    detail: str


def _ols_slope(points: Sequence[tuple[float, float]], *, subject: str) -> float:
    """Closed-form OLS slope of ``y = slope * t`` (no intercept term fit? —
    intercept fitted, slope returned; the same math as the CP/CS fits)."""
    n = float(len(points))
    sum_t = math.fsum(t for t, _ in points)
    sum_y = math.fsum(y for _, y in points)
    sum_tt = math.fsum(t * t for t, _ in points)
    sum_ty = math.fsum(t * y for t, y in points)
    denominator = n * sum_tt - sum_t * sum_t
    if denominator == 0.0:
        raise ValueError(
            f"degenerate {subject} fit: all sessions share the same date, "
            "a slope over time is undetermined"
        )
    return (n * sum_ty - sum_t * sum_y) / denominator


def durability_trend(
    sessions: Sequence[DurabilitySessionPoint],
    *,
    min_long_session_seconds: float = DEFAULT_MIN_LONG_SESSION_SECONDS,
    window_days: int = DEFAULT_TREND_WINDOW_DAYS,
    min_sessions: int = DEFAULT_MIN_TREND_SESSIONS,
) -> DurabilityTrend:
    """Durability trend over the athlete's LONG sessions in a window (RID-9).

    Eligibility (evaluated in this order; every excluded session is
    reported in ``ineligible`` with its reason):

    1. ``outside_window``: the session date is before the trailing window
       ``[window_end - (window_days - 1) days, window_end]``, anchored on
       the LATEST session date among ALL passed sessions.
    2. ``below_min_duration``: ``duration_s`` below
       ``min_long_session_seconds`` (the boundary itself is ELIGIBLE —
       inclusive, compared tolerantly). SHORT sessions are excluded
       because their decoupling measures pacing and warm-up transients,
       not the prolonged-exercise durability trait (rationale in the
       module docstring; Maunder et al. 2021).
    3. ``no_decoupling_value``: ``decoupling`` is ``None`` (not computable
       upstream — reported, never substituted).

    With at least ``min_sessions`` eligible sessions the trend statistic is
    the closed-form OLS slope of Pa:HR over the session date (decoupling
    fraction per day; the same math as the CP/CS fits in
    :mod:`app.engine.zones`), with the descriptive sign-based direction.
    Below the minimum: explicit ``insufficient_data`` with counts. All
    eligible sessions share one date -> ``ValueError`` (a slope over time
    is undetermined).

    Validation: an empty ``sessions`` list, a non-finite or non-positive
    duration, a non-finite decoupling value, a non-positive
    ``min_long_session_seconds``, a non-positive ``window_days``, or
    ``min_sessions < 2`` raise ``ValueError``.

    Reference: Maunder et al. 2021 (durability as a determinant of
    endurance performance; PROJECT_BRIEF section 7.6 seed list); Friel
    (Pa:HR convention).
    """
    if not math.isfinite(min_long_session_seconds) or min_long_session_seconds <= 0.0:
        raise ValueError(
            "min_long_session_seconds must be finite and positive, got "
            f"{min_long_session_seconds!r}"
        )
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days!r}")
    if min_sessions < 2:
        raise ValueError(
            "min_sessions must be at least 2 (a slope over time needs at "
            f"least two distinct dates), got {min_sessions!r}"
        )
    session_list = list(sessions)
    if not session_list:
        raise ValueError("no sessions provided; nothing to trend")

    for point in session_list:
        if not math.isfinite(point.duration_s) or point.duration_s <= 0.0:
            raise ValueError(
                f"session duration must be finite and positive, got "
                f"{point.duration_s!r} s ({point.session_date})"
            )
        if point.decoupling is not None and not math.isfinite(point.decoupling):
            raise ValueError(
                f"session decoupling must be finite or None, got "
                f"{point.decoupling!r} ({point.session_date})"
            )

    window_end = max(point.session_date for point in session_list)
    window_start = window_end - dt.timedelta(days=window_days - 1)

    eligible: list[DurabilitySessionPoint] = []
    ineligible: list[IneligibleSession] = []
    for point in session_list:
        if point.session_date < window_start:
            ineligible.append(
                IneligibleSession(
                    session_date=point.session_date,
                    duration_s=point.duration_s,
                    reason="outside_window",
                    detail=(
                        f"session date {point.session_date} precedes the "
                        f"{window_days}-day trailing window "
                        f"({window_start} .. {window_end})"
                    ),
                )
            )
        elif point.duration_s < min_long_session_seconds and not _same_boundary(
            point.duration_s, min_long_session_seconds
        ):
            ineligible.append(
                IneligibleSession(
                    session_date=point.session_date,
                    duration_s=point.duration_s,
                    reason="below_min_duration",
                    detail=(
                        f"duration {point.duration_s:.0f} s is below the "
                        f"'long session' threshold "
                        f"{min_long_session_seconds:.0f} s (90 min default): "
                        "short sessions' decoupling measures pacing, not "
                        "aerobic durability"
                    ),
                )
            )
        elif point.decoupling is None:
            ineligible.append(
                IneligibleSession(
                    session_date=point.session_date,
                    duration_s=point.duration_s,
                    reason="no_decoupling_value",
                    detail=(
                        "no decoupling value was computable for this session "
                        "(reported, never substituted)"
                    ),
                )
            )
        else:
            eligible.append(point)

    if len(eligible) < min_sessions:
        return DurabilityTrend(
            status="insufficient_data",
            slope_per_day=None,
            direction=None,
            n_eligible=len(eligible),
            n_ineligible=len(ineligible),
            min_long_session_seconds=min_long_session_seconds,
            min_sessions=min_sessions,
            window_days=window_days,
            window_start=window_start,
            window_end=window_end,
            eligible=tuple(eligible),
            ineligible=tuple(ineligible),
            detail=(
                f"insufficient data: {len(eligible)} eligible long session(s) "
                f"in the {window_days}-day window but at least {min_sessions} "
                "are required for a trend; "
                f"{len(ineligible)} session(s) were ineligible "
                f"({ _ineligible_summary(ineligible) }). No trend is "
                "reported rather than a value computed from too few points."
            ),
        )

    origin = min(point.session_date for point in eligible)
    points: list[tuple[float, float]] = []
    for point in eligible:
        decoupling = point.decoupling
        assert decoupling is not None  # eligibility guarantees a value
        points.append((float((point.session_date - origin).days), decoupling))
    slope = _ols_slope(points, subject="durability trend")
    # Sign-based descriptive direction; a slope within float dust of zero
    # (tolerantly compared, the engine's standard boundary rule) is stable.
    if _same_boundary(slope, 0.0):
        direction: TrendDirection = "stable"
    elif slope < 0.0:
        direction = "improving"
    else:
        direction = "worsening"
    return DurabilityTrend(
        status="trend",
        slope_per_day=slope,
        direction=direction,
        n_eligible=len(eligible),
        n_ineligible=len(ineligible),
        min_long_session_seconds=min_long_session_seconds,
        min_sessions=min_sessions,
        window_days=window_days,
        window_start=window_start,
        window_end=window_end,
        eligible=tuple(eligible),
        ineligible=tuple(ineligible),
        detail=(
            f"OLS slope of Pa:HR over {len(eligible)} eligible long session(s) "
            f"in the {window_days}-day window ({window_start} .. {window_end}): "
            f"{slope:+.6f} decoupling fraction per day ({direction}: a "
            "NEGATIVE slope means Pa:HR falls over time — the athlete holds "
            "EF longer — a POSITIVE slope means decoupling worsens). "
            f"{len(ineligible)} session(s) were ineligible "
            f"({_ineligible_summary(ineligible)}). Descriptive only; "
            f"{min_long_session_seconds:.0f} s 'long' threshold and "
            f"{min_sessions}-session minimum are owner-configurable."
        ),
    )


def _ineligible_summary(ineligible: Sequence[IneligibleSession]) -> str:
    """One-line summary of the ineligible sessions' reasons."""
    if not ineligible:
        return "none"
    counts: dict[str, int] = {}
    for item in ineligible:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return ", ".join(f"{reason} x {count}" for reason, count in counts.items())
