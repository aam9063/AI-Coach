"""3-zone intensity distribution: mapping, weekly aggregation, pattern
comparison (RID-5/6/7, PROJECT_BRIEF section 7.5).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core`` (PROJECT_BRIEF sections 6 and 14). Sessions come in as explicit
frozen :class:`ZoneSession` values (date, sport, per-source-zone seconds);
windows, thresholds, reference bands and the minimum pattern volume are
parameters. Side effects, persistence and settings reads belong in callers
(mirrored in Settings with source comments in RID-10, like the
LOAD-11/ZON-11 patterns).

The 3-zone model and its documented mapping (RID-6, section 7.5)
---------------------------------------------------------------
Section 7.5: "3-zone model: Z1 below first threshold, Z2 between
thresholds, Z3 above second threshold. Mapping from 5/7-zone tables
documented in code." The 3-zone construct is the polarized-literature model
(Seiler 2010, "The polarized training model", and the lactate-threshold
framework it builds on): the FIRST threshold is LT1 (aerobic/lactate
turnpoint 1) and the SECOND threshold is LT2 (the lactate threshold itself).

The mapping rule, applied uniformly to Feature 4's published zone tables
(:mod:`app.engine.zones` — the single source of truth for the tables):

    first threshold  = TOP OF SOURCE Z2 (the last fully aerobic zone)
    second threshold = TOP OF SOURCE Z4 (the top of the published
                       "Threshold" zone)

yielding, per modality (percentages of the table's own basis):

    ===============  ==========  ==========  =============================
    modality         first (t1)  second (t2) basis
    ===============  ==========  ==========  =============================
    ``bike_power``   76.0        106.0       % FTP  (Coggan 7-zone table)
    ``run_hr``       90.0        100.0       % LTHR (Friel run 7-zone)
    ``bike_hr``      90.0        100.0       % LTHR (Friel bike 7-zone)
    ``swim_pace``    95.0        100.0       % CSS  (owner 5-band table)
    ===============  ==========  ==========  =============================

so the source-zone-to-3-zone mapping is:

    bike_power : Z1->Z1, Z2->Z1, Z3->Z2, Z4->Z2, Z5->Z3, Z6->Z3, Z7->Z3
    run_hr     : Z1->Z1, Z2->Z1, Z3->Z2, Z4->Z2, Z5a->Z3, Z5b->Z3, Z5c->Z3
    bike_hr    : identical to run_hr
    swim_pace  : Z1->Z1, Z2->Z1, Z3->Z2, Z4->Z3, Z5->Z3

SWIM DIRECTION NOTE: the swim table lives on the PACE axis, which inverts
the intensity axis — a FASTER pace (fewer seconds per 100 m) is a HIGHER
intensity. "Above the second threshold" for swimming therefore means
FASTER than CSS, i.e. swim source Z4 (>= 100% CSS) and Z5 (>= 105% CSS)
map to 3-zone Z3 (the same classifier logic as the power/HR tables, which
both run "higher number = harder").

Provenance (LITERATURE vs OWNER CHOICE, per "Configurable constants")
---------------------------------------------------------------------
- Second thresholds are LITERATURE cut points: Friel defines LTHR as the
  lactate threshold (the published Z4/Z5a boundary IS LT2 at 100% LTHR);
  CSS is the threshold intensity for swimming (Wakayoshi et al. 1992), so
  100% CSS is the published cut point; for power, the top of the published
  Coggan "Threshold" zone (Z4/Z5 boundary) is the threshold cut point of
  the 3-zone adaptation (Allen & Coggan, "Training and Racing with a Power
  Meter"; Seiler 2010 for the 3-zone construct).
- First thresholds are OWNER CHOICE: no source publishes LT1 as an exact
  percentage of FTP/LTHR/CSS. The top of the table's last fully aerobic
  zone is the documented owner choice (76% FTP, 90% LTHR both sports,
  95% CSS), configurable per call — never buried. The swim band structure
  itself is the owner's documented choice (see
  :data:`app.engine.zones.DEFAULT_SWIM_ZONE_BOUNDARY_PCTS`).
- Which source zone belongs to which sport (bike rides -> Coggan power,
  runs -> Friel run HR, swims -> CSS pace) is a documented OWNER CHOICE
  (:data:`SPORT_MODALITY`); an engine-internal extension point covers
  future HR-based bike sessions.

Overriding a threshold recomputes the mapping against the source table's
percentage bounds. A threshold that does not sit ON a source-zone boundary
(it would straddle a zone, forcing an arbitrary split of that zone's
seconds) raises ``ValueError`` — as does every unknown modality, sport or
zone key: nothing is silently dropped.

Weekly aggregation (RID-5, section 7.5)
---------------------------------------
Time in zone per sport per week, aggregated from per-source-zone seconds
via the 3-zone mapping. Documented week semantics:

- Week boundary: ISO 8601 weeks, MONDAY start (:func:`iso_week_start`
  returns the Monday; :func:`iso_week_of` the ``(iso_year, iso_week)``).
- Date basis: the session's calendar date AS STORED (the athlete's local
  date from ingestion). The engine never converts timezones.
- Session-to-week attribution: a session's seconds are attributed WHOLLY
  to the ISO week containing its date. Multi-week sessions (e.g. a 5-hour
  ride crossing midnight) are attributed entirely to their start week —
  the documented OWNER CHOICE, matching the ingestion model of one row per
  session; no pro-rating is attempted.
- The report covers every ISO week from the earliest session's week
  through the latest, inclusive, so interior weeks WITHOUT sessions are
  explicit ``"no_data"`` entries — never a fabricated 0% split (their
  ``percentages`` are ``None``).
- A week with sessions but zero total seconds (e.g. only sessions without
  intensity data) is ``"data"`` with ``percentages is None``: there is
  nothing to divide by.
- Unknown sport, unknown zone key, negative or non-finite seconds raise
  ``ValueError`` (never silent).

Descriptive pattern comparison (RID-7, section 7.5)
----------------------------------------------------
"Report vs polarized (~80/20) and pyramidal distributions (Seiler 2010;
Stöggl & Sperlich 2014). Describe, do not moralize."
:func:`descriptive_pattern_comparison` is DESCRIPTIVE ONLY: it reports the
observed Z1/Z2/Z3 percentages and whether they sit within the reference
bands of the two named patterns, with a neutral label and a fixed caveat.
There is NO advice, NO recommendation, NO quality judgement and NO
composite adherence score anywhere in the output — asserted by a
forbidden-word test. A MINIMUM-VOLUME rule (below
:data:`DEFAULT_MIN_PATTERN_WEEK_SECONDS` of total time in zone) refuses to
label a week at all: at low weekly volume the percentages reflect session
choice more than distribution. The reference bands are documented,
owner-reviewable OWNER CHOICES around the published point values (the
papers report means, not decision bands).

Formulas
--------
    share_of_zone(z) = z_seconds / (z1 + z2 + z3) * 100   (percent of week)
    pattern label    = polarized   if shares within polarized bands only
                     = pyramidal   if shares within pyramidal bands only
                     = mixed       if within both, or within neither
                     = insufficient_volume  if total < minimum volume

All sums use :func:`math.fsum`; the three zones partition the mapped time
exactly (``z1 + z2 + z3 == total``, asserted by the tests — no lost or
double-counted seconds). All result objects are frozen and slotted, carry
only primitives (Pydantic-ready for the future ``get_intensity_distribution``
tool, section 9.3), and validation failures raise ``ValueError`` — the
engine never silently drops or fabricates data.
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.engine.zones import hr_zones, power_zones, swim_zones

# ---------------------------------------------------------------------------
# Configurable constants (RID-5/6/7, §7.5/§14) — provenance in the module
# docstring; every entry is LITERATURE-cited or an explicit OWNER CHOICE.
# ---------------------------------------------------------------------------

_SAME_BOUNDARY_REL_TOL: Final = 1e-12
_SAME_BOUNDARY_ABS_TOL: Final = 1e-9


def _same_boundary(a: float, b: float) -> bool:
    """Whether two percentage bounds are the same boundary value.

    The same tolerant-comparison rule as the zone tables
    (:func:`app.engine.zones._same_boundary`): a caller who computes a
    boundary (``0.95 * 100`` vs the table literal ``95.0``) must land on
    the same side, and float noise at an exact boundary must not flip a
    classification.
    """
    return math.isclose(a, b, rel_tol=_SAME_BOUNDARY_REL_TOL,
                        abs_tol=_SAME_BOUNDARY_ABS_TOL)


SportKey = Literal["run", "bike", "swim"]
"""Stable machine-readable keys of the three sports (section 7.5)."""

SPORT_KEYS: Final[tuple[SportKey, ...]] = ("run", "bike", "swim")
"""All sport keys (the stable, validated sport namespace)."""

IntensityModalityKey = Literal["bike_power", "run_hr", "bike_hr", "swim_pace"]
"""Source zone-table modality keys: which Feature 4 table the session
seconds come from. ``bike_power`` = Coggan 7-zone (% FTP), ``run_hr`` /
``bike_hr`` = Friel 7-zone per sport (% LTHR), ``swim_pace`` = the CSS
5-band table (% CSS speed)."""

INTENSITY_MODALITY_KEYS: Final[tuple[IntensityModalityKey, ...]] = (
    "bike_power",
    "run_hr",
    "bike_hr",
    "swim_pace",
)
"""All intensity modality keys (the stable, validated modality namespace)."""

IntensityZoneKey = Literal["Z1", "Z2", "Z3"]
"""The three zone keys of the 3-zone model (section 7.5)."""

INTENSITY_ZONE_KEYS: Final[tuple[IntensityZoneKey, ...]] = ("Z1", "Z2", "Z3")
"""All 3-zone keys (the stable, validated zone namespace)."""

SPORT_MODALITY: Final[dict[SportKey, IntensityModalityKey]] = {
    "run": "run_hr",
    "bike": "bike_power",
    "swim": "swim_pace",
}
"""Default source table per sport. OWNER CHOICE (documented): bike rides
use the Coggan power table, runs the Friel run HR table, swims the CSS pace
table — the primary intensity modality of each sport in the brief
(sections 7.3/7.5). EXTENSIBILITY: a future HR-based bike session needs one
entry here (e.g. a per-session modality override at the caller layer), not
a change to the mapping logic."""

DEFAULT_FIRST_THRESHOLD_PCTS: Final[dict[IntensityModalityKey, float]] = {
    "bike_power": 76.0,
    "run_hr": 90.0,
    "bike_hr": 90.0,
    "swim_pace": 95.0,
}
"""First-threshold (LT1) cut points, in % of each table's basis: the TOP OF
SOURCE Z2 (last fully aerobic zone) of each published table. OWNER CHOICE
(see module docstring): no source publishes LT1 as an exact percentage.
Configurable per call via ``three_zone_model(first_threshold_pct=...)``."""

DEFAULT_SECOND_THRESHOLD_PCTS: Final[dict[IntensityModalityKey, float]] = {
    "bike_power": 106.0,
    "run_hr": 100.0,
    "bike_hr": 100.0,
    "swim_pace": 100.0,
}
"""Second-threshold (LT2) cut points: the TOP OF THE PUBLISHED THRESHOLD
ZONE of each table (Coggan Z4/Z5 boundary; Friel Z4/Z5a boundary = 100%
LTHR; the Tempo/CSS boundary = 100% CSS). LITERATURE cut points (see
module docstring). Configurable per call via
``three_zone_model(second_threshold_pct=...)``."""

ThresholdProvenance = Literal["literature", "owner_choice"]
"""Provenance tag of a threshold cut point (reviewed by the owner)."""

_THRESHOLD_PROVENANCE: Final[
    dict[IntensityModalityKey, tuple[ThresholdProvenance, str,
                                     ThresholdProvenance, str]]
] = {
    "bike_power": (
        "owner_choice",
        "OWNER CHOICE: no source publishes LT1 as an exact % FTP; the "
        "published Coggan Z2/Z3 boundary (top of Z2) is used as the "
        "first-threshold cut point (Allen & Coggan, 'Training and Racing "
        "with a Power Meter'; Seiler 2010 for the 3-zone construct).",
        "literature",
        "LITERATURE: the top of the published Coggan Threshold zone "
        "(Z4/Z5 boundary) is the second-threshold (LT2) cut point of the "
        "3-zone adaptation (Allen & Coggan; Seiler 2010).",
    ),
    "run_hr": (
        "owner_choice",
        "OWNER CHOICE: Friel publishes no LT1 percentage; the top of the "
        "published Friel run Z2 (90% LTHR) is used as the first-threshold "
        "cut point.",
        "literature",
        "LITERATURE: Friel defines LTHR as the lactate threshold, so the "
        "top of the published Z4 (100% LTHR) is the published "
        "second-threshold cut point (Friel, 'The Triathlete's Training "
        "Bible').",
    ),
    "bike_hr": (
        "owner_choice",
        "OWNER CHOICE: Friel publishes no LT1 percentage; the top of the "
        "published Friel bike Z2 (90% LTHR) is used as the first-threshold "
        "cut point.",
        "literature",
        "LITERATURE: Friel defines LTHR as the lactate threshold, so the "
        "top of the published Z4 (100% LTHR) is the published "
        "second-threshold cut point (Friel, 'The Triathlete's Training "
        "Bible').",
    ),
    "swim_pace": (
        "owner_choice",
        "OWNER CHOICE: the brief publishes no swim zone table; the top of "
        "the owner-configured Aerobic band (95% CSS) is used as the "
        "first-threshold cut point.",
        "literature",
        "LITERATURE: CSS is the threshold intensity (Wakayoshi et al. "
        "1992), so 100% CSS — the top of the Tempo band — is the published "
        "second-threshold cut point.",
    ),
}
"""Provenance tag and citation for each modality's two thresholds, in the
order (first tag, first citation, second tag, second citation)."""

ReferenceBands = tuple[tuple[float, float], tuple[float, float],
                       tuple[float, float]]
"""Three (lo, hi) percentage bands, one per 3-zone key Z1/Z2/Z3."""

DEFAULT_POLARIZED_BANDS: Final[ReferenceBands] = (
    (70.0, 90.0),
    (0.0, 15.0),
    (10.0, 30.0),
)
"""Reference bands of the POLARIZED pattern (Seiler 2010, ~80/20: ~80% of
training below the first threshold, very little between the thresholds,
~20% above the second). OWNER-REVIEWABLE bands around the published point
values (the literature reports means, not decision bands); configurable per
call via ``polarized_bands``."""

DEFAULT_PYRAMIDAL_BANDS: Final[ReferenceBands] = (
    (55.0, 75.0),
    (15.0, 35.0),
    (5.0, 20.0),
)
"""Reference bands of the PYRAMIDAL pattern (Stöggl & Sperlich 2014: Z1
largest, Z2 moderate, Z3 smallest — the classic pyramid). OWNER-REVIEWABLE
bands around the published point values; configurable per call via
``pyramidal_bands``."""

DEFAULT_MIN_PATTERN_WEEK_SECONDS: Final[float] = 7200.0
"""Minimum weekly total time in zone (2 h) before a pattern label is
reported at all. OWNER CHOICE (the brief fixes no number, section 7.5):
below this volume the weekly percentages reflect session choice more than
distribution, so no reference-pattern label is reported. Configurable per
call via ``min_pattern_week_seconds``; mirrored in Settings in RID-10."""

# ---------------------------------------------------------------------------
# Source tables (Feature 4, app.engine.zones) as (key, lo_pct, hi_pct)
# ---------------------------------------------------------------------------


def _source_pct_bounds(
    modality: IntensityModalityKey,
) -> tuple[tuple[str, float | None, float | None], ...]:
    """(key, min_pct, max_pct) bounds of one source table, from Feature 4.

    The tables are built at a basis of 100 so the absolute bounds equal the
    percentage bounds (power at FTP 100 W, HR at LTHR 100 bpm, swim at CSS
    1 m/s); :mod:`app.engine.zones` stays the single source of truth.
    """
    if modality == "bike_power":
        return tuple(
            (zone.key, zone.min_pct_ftp, zone.max_pct_ftp)
            for zone in power_zones(100.0)
        )
    if modality in ("run_hr", "bike_hr"):
        sport: Final = "run" if modality == "run_hr" else "bike"
        return tuple(
            (zone.key, zone.min_pct_lthr, zone.max_pct_lthr)
            for zone in hr_zones(sport, 100.0)
        )
    return tuple(
        (zone.key, zone.min_pct_css, zone.max_pct_css)
        for zone in swim_zones(1.0)
    )


# ---------------------------------------------------------------------------
# RID-6: the 3-zone model and the mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThreeZoneSeconds:
    """Seconds mapped into the three zones of the 3-zone model (RID-6).

    ``total_seconds`` is :func:`math.fsum` of the three zone totals — the
    three zones partition the mapped time exactly (no lost or
    double-counted seconds; asserted by the tests). Frozen and slotted.
    """

    z1_seconds: float
    z2_seconds: float
    z3_seconds: float
    total_seconds: float


@dataclass(frozen=True, slots=True)
class ThreeZoneModel:
    """The documented mapping of one source zone table to 3 zones (RID-6).

    ``first_threshold_pct`` / ``second_threshold_pct`` are the LT1/LT2 cut
    points in % of the source table's basis (FTP / LTHR / CSS), each with
    its provenance tag and a citation in ``detail`` (LITERATURE where a
    published cut point exists, OWNER CHOICE otherwise — see the module
    docstring). ``mapping`` lists ``(source_key, three_zone_key)`` pairs in
    ascending source-zone order. :meth:`zone_for` classifies one source
    zone key and raises ``ValueError`` on an unknown key — a source zone is
    never silently dropped. Frozen and slotted.
    """

    modality: IntensityModalityKey
    source_zone_keys: tuple[str, ...]
    first_threshold_pct: float
    second_threshold_pct: float
    first_threshold_provenance: ThresholdProvenance
    second_threshold_provenance: ThresholdProvenance
    mapping: tuple[tuple[str, IntensityZoneKey], ...]
    detail: str

    def zone_for(self, source_key: str) -> IntensityZoneKey:
        """The 3-zone key of ``source_key``; unknown keys raise
        ``ValueError`` (never silently dropped)."""
        for key, zone_key in self.mapping:
            if key == source_key:
                return zone_key
        raise ValueError(
            f"unknown source zone key {source_key!r} for modality "
            f"{self.modality!r}; expected one of {self.source_zone_keys}"
        )


def _band_of_zone(
    lo_pct: float | None, hi_pct: float | None, t1: float, t2: float
) -> IntensityZoneKey:
    """Which 3-zone band one source zone's percentage bounds fall in.

    Z1 when the zone lies at or below the first threshold (its top on the
    boundary joins the lower band, matching the tables' exclusive upper
    edges), Z3 when it lies at or above the second threshold, Z2 between.
    """
    if hi_pct is not None and (hi_pct < t1 or _same_boundary(hi_pct, t1)):
        return "Z1"
    if lo_pct is not None and (lo_pct > t2 or _same_boundary(lo_pct, t2)):
        return "Z3"
    return "Z2"


def _require_boundary_threshold(
    zones: tuple[tuple[str, float | None, float | None], ...],
    t: float,
    which: str,
) -> None:
    """A threshold must sit ON a source-zone boundary, never inside one.

    A threshold strictly inside a zone's bounds would straddle that zone
    and force an arbitrary split of its seconds — raise instead.
    """
    for key, lo, hi in zones:
        on_edge = (lo is not None and _same_boundary(lo, t)) or (
            hi is not None and _same_boundary(hi, t)
        )
        if on_edge:
            return
        if (lo is None or lo < t) and (hi is None or hi > t):
            raise ValueError(
                f"{which} threshold {t!r}% falls inside source zone {key!r} "
                f"({lo!r}-{hi!r}%); thresholds must sit on a source-zone "
                "boundary so no zone's seconds are split arbitrarily"
            )


def three_zone_model(
    modality: IntensityModalityKey,
    *,
    first_threshold_pct: float | None = None,
    second_threshold_pct: float | None = None,
) -> ThreeZoneModel:
    """The documented 3-zone mapping for one source zone table (RID-6).

    See the module docstring for the mapping rule, the provenance split and
    the per-modality cut points. ``first_threshold_pct`` /
    ``second_threshold_pct`` default to :data:`DEFAULT_FIRST_THRESHOLD_PCTS`
    / :data:`DEFAULT_SECOND_THRESHOLD_PCTS` and may be overridden per call
    (the mapping is recomputed against the source table's bounds). An
    unknown ``modality``, a non-positive threshold, ``t1 >= t2`` or a
    threshold that does not sit on a source-zone boundary raises
    ``ValueError``.
    """
    if modality not in INTENSITY_MODALITY_KEYS:
        raise ValueError(
            f"unknown intensity modality {modality!r}; expected one of "
            f"{INTENSITY_MODALITY_KEYS}"
        )
    zones = _source_pct_bounds(modality)
    t1 = (
        DEFAULT_FIRST_THRESHOLD_PCTS[modality]
        if first_threshold_pct is None else first_threshold_pct
    )
    t2 = (
        DEFAULT_SECOND_THRESHOLD_PCTS[modality]
        if second_threshold_pct is None else second_threshold_pct
    )
    if t1 <= 0.0 or t2 <= 0.0:
        raise ValueError(
            f"thresholds must be positive, got {t1!r} and {t2!r}"
        )
    if t2 <= t1 or _same_boundary(t1, t2):
        raise ValueError(
            f"the second threshold ({t2!r}%) must be strictly above the "
            f"first ({t1!r}%)"
        )
    _require_boundary_threshold(zones, t1, "first")
    _require_boundary_threshold(zones, t2, "second")
    mapping = tuple(
        (key, _band_of_zone(lo, hi, t1, t2)) for key, lo, hi in zones
    )
    p1, cite1, p2, cite2 = _THRESHOLD_PROVENANCE[modality]
    direction = (
        " DIRECTION NOTE: the swim pace axis INVERTS the intensity axis — "
        "above the second threshold means FASTER than CSS, so swim Z4/Z5 "
        "map to 3-zone Z3."
        if modality == "swim_pace" else ""
    )
    detail = (
        f"3-zone mapping for {modality!r}: first threshold (LT1) = top of "
        f"source Z2 at {t1!r}%, second threshold (LT2) = top of the "
        f"published Threshold zone at {t2!r}%. {cite1} {cite2}{direction} "
        "Mapping: " + ", ".join(f"{k}->{b}" for k, b in mapping) + "."
    )
    return ThreeZoneModel(
        modality=modality,
        source_zone_keys=tuple(key for key, _, _ in zones),
        first_threshold_pct=t1,
        second_threshold_pct=t2,
        first_threshold_provenance=p1,
        second_threshold_provenance=p2,
        mapping=mapping,
        detail=detail,
    )


def map_to_three_zones(
    model: ThreeZoneModel, source_seconds: Mapping[str, float]
) -> ThreeZoneSeconds:
    """Aggregate per-source-zone seconds into the 3 zones via ``model``.

    Every source key must be a key of the model's table (unknown keys raise
    ``ValueError`` — never silently dropped) and every value must be finite
    and non-negative (``ValueError``). The three zone totals partition the
    mapped time exactly: ``z1 + z2 + z3 == total`` (no lost or
    double-counted seconds).
    """
    zone_buckets: Final[dict[IntensityZoneKey, list[float]]] = {
        "Z1": [], "Z2": [], "Z3": [],
    }
    for key, seconds in source_seconds.items():
        zone_key = model.zone_for(key)  # unknown key -> ValueError
        if not math.isfinite(seconds):
            raise ValueError(
                f"zone seconds for {key!r} must be finite, got {seconds!r}"
            )
        if seconds < 0.0:
            raise ValueError(
                f"zone seconds for {key!r} must not be negative, got "
                f"{seconds!r}"
            )
        zone_buckets[zone_key].append(seconds)
    z1 = math.fsum(zone_buckets["Z1"])
    z2 = math.fsum(zone_buckets["Z2"])
    z3 = math.fsum(zone_buckets["Z3"])
    return ThreeZoneSeconds(
        z1_seconds=z1, z2_seconds=z2, z3_seconds=z3,
        total_seconds=math.fsum((z1, z2, z3)),
    )


# ---------------------------------------------------------------------------
# RID-5: weekly time-in-zone aggregation per sport
# ---------------------------------------------------------------------------

WeekStatus = Literal["data", "no_data"]
"""Status of one sport-week: ``"data"`` when the week holds at least one
session of the sport, ``"no_data"`` otherwise (never a fabricated 0%
split)."""


@dataclass(frozen=True, slots=True)
class ZoneSession:
    """One training session's time in the source zones (RID-5 input).

    Construct via :func:`zone_session`, which validates the sport, the
    zone keys against the sport's source table and the seconds (finite,
    non-negative). ``zone_seconds`` is the validated ``((key, seconds),
    ...)`` pairs in the source table's canonical order; a session without
    intensity data is an EMPTY mapping (documented, allowed). Frozen and
    slotted.
    """

    session_date: dt.date
    sport: SportKey
    zone_seconds: tuple[tuple[str, float], ...]
    total_seconds: float


@dataclass(frozen=True, slots=True)
class WeeklySportZones:
    """One sport's 3-zone time in one ISO week (RID-5 output).

    ``status`` is ``"no_data"`` when the week holds no session of the sport
    — the seconds are then all ``0.0`` and ``percentages`` is ``None``: a
    week without data is reported AS "no data", never as a fabricated 0%
    split. A ``"data"`` week with zero total seconds (only sessions without
    intensity data) also carries ``percentages is None`` (nothing to divide
    by). Percentages are exact ``seconds / total * 100``. Frozen and
    slotted.
    """

    sport: SportKey
    status: WeekStatus
    z1_seconds: float
    z2_seconds: float
    z3_seconds: float
    total_seconds: float
    percentages: tuple[float, float, float] | None


@dataclass(frozen=True, slots=True)
class WeeklyIntensity:
    """One ISO week's 3-zone time per sport (RID-5 output).

    ``week_start`` is the Monday (ISO 8601). ``sports`` carries one
    :class:`WeeklySportZones` per requested sport, in the requested order
    (canonical :data:`SPORT_KEYS` order by default). Frozen and slotted.
    """

    iso_year: int
    iso_week: int
    week_start: dt.date
    sports: tuple[WeeklySportZones, ...]


def iso_week_of(day: dt.date) -> tuple[int, int]:
    """The ``(iso_year, iso_week)`` of ``day`` (ISO 8601, Monday start)."""
    iso = day.isocalendar()
    return (iso.year, iso.week)


def iso_week_start(iso_year: int, iso_week: int) -> dt.date:
    """The Monday of ISO week ``iso_week`` of ``iso_year`` (section 7.5).

    Raises ``ValueError`` for a week number that does not exist in that ISO
    year (``dt.date.fromisocalendar`` is the authority).
    """
    return dt.date.fromisocalendar(iso_year, iso_week, 1)


def _validated_zone_seconds(
    sport: SportKey, zone_seconds: Mapping[str, float]
) -> tuple[tuple[str, float], ...]:
    """Validate zone seconds against the sport's source table (RID-5).

    Unknown sport, unknown zone key, non-finite or negative seconds raise
    ``ValueError``; the validated pairs are returned in the source table's
    canonical order so aggregation is order-independent.
    """
    if sport not in SPORT_KEYS:
        raise ValueError(f"unknown sport {sport!r}; expected one of {SPORT_KEYS}")
    model = three_zone_model(SPORT_MODALITY[sport])
    valid = set(model.source_zone_keys)
    pairs: list[tuple[str, float]] = []
    for key, seconds in zone_seconds.items():
        if key not in valid:
            raise ValueError(
                f"unknown zone key {key!r} for sport {sport!r}; expected one "
                f"of {model.source_zone_keys}"
            )
        if not math.isfinite(seconds):
            raise ValueError(
                f"zone seconds for {key!r} must be finite, got {seconds!r}"
            )
        if seconds < 0.0:
            raise ValueError(
                f"zone seconds for {key!r} must not be negative, got "
                f"{seconds!r}"
            )
        pairs.append((key, seconds))
    pairs.sort(key=lambda pair: model.source_zone_keys.index(pair[0]))
    return tuple(pairs)


def zone_session(
    session_date: dt.date,
    sport: SportKey,
    zone_seconds: Mapping[str, float],
) -> ZoneSession:
    """Build a validated :class:`ZoneSession` (RID-5 input).

    ``sport`` must be one of :data:`SPORT_KEYS`, every zone key must belong
    to the sport's source table and every seconds value must be finite and
    non-negative — otherwise ``ValueError`` (unknown keys are never
    silently dropped). An empty ``zone_seconds`` is allowed (a session
    without intensity data contributes zero seconds).
    """
    pairs = _validated_zone_seconds(sport, zone_seconds)
    return ZoneSession(
        session_date=session_date,
        sport=sport,
        zone_seconds=pairs,
        total_seconds=math.fsum(seconds for _, seconds in pairs),
    )


def weekly_time_in_zone(
    sessions: Sequence[ZoneSession],
    *,
    sports: Sequence[SportKey] | None = None,
) -> tuple[WeeklyIntensity, ...]:
    """Time in zone per sport per ISO week (RID-5, section 7.5).

    Aggregates the sessions' per-source-zone seconds into the 3-zone model
    (:data:`SPORT_MODALITY`) and reports, for EVERY ISO week from the
    earliest session's week through the latest inclusive, one
    :class:`WeeklyIntensity` with one :class:`WeeklySportZones` per
    requested sport. Week semantics (documented in the module docstring):
    ISO 8601 weeks with Monday start; whole-session attribution to the ISO
    week of the session date (multi-week sessions are never split); weeks
    without a session of a sport report ``"no_data"`` with
    ``percentages is None`` — never a fabricated 0% split.

    ``sports`` defaults to all of :data:`SPORT_KEYS` in canonical order; an
    unknown sport raises ``ValueError``. An empty ``sessions`` sequence,
    an unknown sport in a session, an unknown zone key or non-finite /
    negative seconds raise ``ValueError`` (defensive re-validation: the
    engine never silently drops data).
    """
    requested = tuple(SPORT_KEYS) if sports is None else tuple(sports)
    for sport in requested:
        if sport not in SPORT_KEYS:
            raise ValueError(
                f"unknown sport {sport!r}; expected one of {SPORT_KEYS}"
            )
    if not sessions:
        raise ValueError("no sessions provided; nothing to aggregate")
    models = {
        sport: three_zone_model(SPORT_MODALITY[sport]) for sport in SPORT_KEYS
    }
    grouped: Final[dict[tuple[int, int], dict[SportKey, list[ZoneSession]]]] = {}
    for session in sessions:
        # Defensive re-validation: the engine never trusts its inputs.
        _validated_zone_seconds(session.sport, dict(session.zone_seconds))
        if session.sport not in SPORT_KEYS:
            raise ValueError(
                f"unknown sport {session.sport!r}; expected one of "
                f"{SPORT_KEYS}"
            )
        grouped.setdefault(iso_week_of(session.session_date), {}) \
               .setdefault(session.sport, []).append(session)

    first_week = min(grouped)
    last_week = max(grouped)
    weeks: list[WeeklyIntensity] = []
    week_start = iso_week_start(*first_week)
    last_start = iso_week_start(*last_week)
    while week_start <= last_start:
        year, week = iso_week_of(week_start)
        week_sessions = grouped.get((year, week), {})
        sport_results: list[WeeklySportZones] = []
        for sport in requested:
            sport_sessions = week_sessions.get(sport, [])
            z1s: list[float] = []
            z2s: list[float] = []
            z3s: list[float] = []
            for session in sport_sessions:
                mapped = map_to_three_zones(
                    models[sport], dict(session.zone_seconds)
                )
                z1s.append(mapped.z1_seconds)
                z2s.append(mapped.z2_seconds)
                z3s.append(mapped.z3_seconds)
            z1, z2, z3 = (math.fsum(z1s), math.fsum(z2s), math.fsum(z3s))
            total = math.fsum((z1, z2, z3))
            sport_results.append(
                WeeklySportZones(
                    sport=sport,
                    status="data" if sport_sessions else "no_data",
                    z1_seconds=z1,
                    z2_seconds=z2,
                    z3_seconds=z3,
                    total_seconds=total,
                    percentages=(
                        (z1 / total * 100.0, z2 / total * 100.0,
                         z3 / total * 100.0)
                        if total > 0.0 else None
                    ),
                )
            )
        weeks.append(
            WeeklyIntensity(
                iso_year=year, iso_week=week, week_start=week_start,
                sports=tuple(sport_results),
            )
        )
        week_start += dt.timedelta(days=7)
    return tuple(weeks)


# ---------------------------------------------------------------------------
# RID-7: descriptive comparison vs reference patterns
# ---------------------------------------------------------------------------

PatternLabel = Literal["polarized", "pyramidal", "mixed", "insufficient_volume"]
"""Neutral descriptive labels (RID-7, section 7.5): which named reference
pattern the observed split sits within — or ``"mixed"`` when it sits within
both bands' overlap or within neither, or ``"insufficient_volume"`` when
the week is too small to label at all. Deliberately NOT an adherence
score."""

DESCRIPTIVE_CAVEAT: Final[str] = (
    "Descriptive comparison only: this states how the observed time-in-zone "
    "split relates to published reference patterns (Seiler 2010; Stöggl & "
    "Sperlich 2014). It is an observation about distribution, not a "
    "judgement of training quality; reference bands and the minimum volume "
    "are owner-configurable, and individual context takes precedence."
)
"""The fixed descriptive-only caveat carried by every comparison result."""


@dataclass(frozen=True, slots=True)
class PatternComparison:
    """A DESCRIPTIVE comparison of one week's 3-zone split (RID-7).

    ``z1_pct`` / ``z2_pct`` / ``z3_pct`` are the observed shares (``None``
    only when the total is zero). ``label`` is the neutral descriptive
    label; ``polarized_within_bands`` / ``pyramidal_within_bands`` are
    ``None`` when the week is below the minimum volume (no label at all).
    ``detail`` states the observation neutrally and ``caveat`` (fixed,
    :data:`DESCRIPTIVE_CAVEAT`) names the descriptive-only nature of the
    output. There is NO advice, NO recommendation, NO quality judgement and
    NO composite adherence score (section 7.5; asserted by a forbidden-word
    test). Frozen and slotted.
    """

    z1_pct: float | None
    z2_pct: float | None
    z3_pct: float | None
    label: PatternLabel
    polarized_within_bands: bool | None
    pyramidal_within_bands: bool | None
    total_seconds: float
    min_pattern_week_seconds: float
    detail: str
    caveat: str = DESCRIPTIVE_CAVEAT


def _validated_reference_bands(
    bands: ReferenceBands, name: str
) -> ReferenceBands:
    """Three (lo, hi) percentage bands with ``0 <= lo <= hi <= 100``."""
    if len(bands) != 3:
        raise ValueError(
            f"{name} must be exactly three (lo, hi) bands, one per zone, "
            f"got {bands!r}"
        )
    validated: list[tuple[float, float]] = []
    for lo, hi in bands:
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise ValueError(f"{name} bounds must be finite, got {bands!r}")
        if not (0.0 <= lo <= hi <= 100.0):
            raise ValueError(
                f"{name} bounds must satisfy 0 <= lo <= hi <= 100, got "
                f"{(lo, hi)!r}"
            )
        validated.append((lo, hi))
    return (
        (validated[0][0], validated[0][1]),
        (validated[1][0], validated[1][1]),
        (validated[2][0], validated[2][1]),
    )


def _within_bands(pcts: tuple[float, float, float], bands: ReferenceBands) -> bool:
    """Whether the (Z1, Z2, Z3) shares sit within their respective bands
    (tolerant at the exact band edges, the same boundary rule as the zone
    tables)."""
    return all(
        (lo <= pct <= hi) or _same_boundary(pct, lo) or _same_boundary(pct, hi)
        for pct, (lo, hi) in zip(pcts, bands, strict=True)
    )


def descriptive_pattern_comparison(
    *,
    z1_seconds: float,
    z2_seconds: float,
    z3_seconds: float,
    min_pattern_week_seconds: float = DEFAULT_MIN_PATTERN_WEEK_SECONDS,
    polarized_bands: ReferenceBands = DEFAULT_POLARIZED_BANDS,
    pyramidal_bands: ReferenceBands = DEFAULT_PYRAMIDAL_BANDS,
) -> PatternComparison:
    """Describe one week's 3-zone split against the reference patterns.

    DESCRIPTIVE ONLY (section 7.5): reports the observed Z1/Z2/Z3
    percentages and whether they sit within the polarized (Seiler 2010,
    ~80/20) and pyramidal (Stöggl & Sperlich 2014) reference bands, with a
    neutral label and the fixed :data:`DESCRIPTIVE_CAVEAT`. No advice, no
    recommendation, no quality judgement, no composite adherence score.

    MINIMUM-VOLUME RULE: a week whose total time in zone is below
    ``min_pattern_week_seconds`` (default
    :data:`DEFAULT_MIN_PATTERN_WEEK_SECONDS`, an OWNER CHOICE — the exact
    boundary itself counts as sufficient, compared tolerantly) is labelled
    ``"insufficient_volume"`` with ``None`` band memberships: at low
    weekly volume the percentages reflect session choice more than
    distribution, so no pattern label is reported. A zero total is also
    ``"insufficient_volume"`` with ``None`` percentages.

    Label semantics: within the polarized bands only -> ``"polarized"``;
    within the pyramidal bands only -> ``"pyramidal"``; within both (band
    overlap) or within neither -> ``"mixed"``. Band edges are compared
    tolerantly (``_same_boundary``), so float noise at an edge cannot flip
    the label. Non-finite or negative seconds, non-finite or invalid
    bands (``0 <= lo <= hi <= 100``) or a negative minimum raise
    ``ValueError``.
    """
    seconds = (z1_seconds, z2_seconds, z3_seconds)
    for name, value in zip(("Z1", "Z2", "Z3"), seconds, strict=True):
        if not math.isfinite(value):
            raise ValueError(f"{name} seconds must be finite, got {value!r}")
        if value < 0.0:
            raise ValueError(
                f"{name} seconds must not be negative, got {value!r}"
            )
    if (
        not math.isfinite(min_pattern_week_seconds)
        or min_pattern_week_seconds < 0.0
    ):
        raise ValueError(
            "min_pattern_week_seconds must be finite and non-negative, got "
            f"{min_pattern_week_seconds!r}"
        )
    polarized = _validated_reference_bands(polarized_bands, "polarized_bands")
    pyramidal = _validated_reference_bands(pyramidal_bands, "pyramidal_bands")

    total = math.fsum(seconds)
    if total <= 0.0:
        return PatternComparison(
            z1_pct=None, z2_pct=None, z3_pct=None,
            label="insufficient_volume",
            polarized_within_bands=None, pyramidal_within_bands=None,
            total_seconds=total,
            min_pattern_week_seconds=min_pattern_week_seconds,
            detail=(
                f"Total time in zone {total:.0f} s is zero; no "
                "distribution can be described."
            ),
        )
    z1_pct, z2_pct, z3_pct = (
        z1_seconds / total * 100.0,
        z2_seconds / total * 100.0,
        z3_seconds / total * 100.0,
    )
    observed = (
        f"Observed split Z1 {z1_pct:.1f}% / Z2 {z2_pct:.1f}% / Z3 "
        f"{z3_pct:.1f}% of {total:.0f} s in zone over the week"
    )
    if total < min_pattern_week_seconds and not _same_boundary(
        total, min_pattern_week_seconds
    ):
        return PatternComparison(
            z1_pct=z1_pct, z2_pct=z2_pct, z3_pct=z3_pct,
            label="insufficient_volume",
            polarized_within_bands=None, pyramidal_within_bands=None,
            total_seconds=total,
            min_pattern_week_seconds=min_pattern_week_seconds,
            detail=(
                f"{observed}; the total is below the minimum pattern "
                f"volume {min_pattern_week_seconds:.0f} s, so no "
                "reference-pattern label is reported: at low weekly "
                "volume the percentages reflect session choice more than "
                "distribution."
            ),
        )
    in_polarized = _within_bands((z1_pct, z2_pct, z3_pct), polarized)
    in_pyramidal = _within_bands((z1_pct, z2_pct, z3_pct), pyramidal)
    if in_polarized and not in_pyramidal:
        label: PatternLabel = "polarized"
        tail = (
            "; the distribution sits within the polarized reference bands "
            "(Seiler 2010, ~80/20)."
        )
    elif in_pyramidal and not in_polarized:
        label = "pyramidal"
        tail = (
            "; the distribution sits within the pyramidal reference bands "
            "(Stöggl & Sperlich 2014: Z1 largest, Z2 moderate, Z3 "
            "smallest)."
        )
    else:
        label = "mixed"
        tail = (
            "; the distribution sits within the overlap of both reference "
            "bands (reported as mixed)."
            if in_polarized and in_pyramidal
            else "; the distribution does not sit within the polarized "
            "(Seiler 2010) or pyramidal (Stöggl & Sperlich 2014) reference "
            "bands and is reported as mixed."
        )
    return PatternComparison(
        z1_pct=z1_pct, z2_pct=z2_pct, z3_pct=z3_pct,
        label=label,
        polarized_within_bands=in_polarized,
        pyramidal_within_bands=in_pyramidal,
        total_seconds=total,
        min_pattern_week_seconds=min_pattern_week_seconds,
        detail=observed + tail,
    )
