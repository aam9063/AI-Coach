"""Mean-maximal power/speed curves, Critical Power (CP/W'), Critical Speed
(CS/D'), FTP resolution, power zones, VDOT training paces, swim Critical
Swim Speed (CSS) with swim zones, Friel heart-rate zones per sport, and
threshold change detection that PROPOSES and never applies (ZON-9).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core.settings`` (PROJECT_BRIEF sections 6 and 14). Thresholds are
explicit parameters; side effects and settings reads belong in callers.

Real-data limitation (ZON-1..ZON-6, ODD ``engine-zones.md``): the owner has
NO power meter, so there is no real power stream to fit. Per PROJECT_BRIEF
section 12.4 the acceptance for the CP/W' fit is validation on SYNTHETIC
data with known parameters (``tests/engine/test_zones_cp.py``), and every
result computed from a real stream later must be treated accordingly.
Consequently the owner's FTP of record is the MANUALLY confirmed 180 W and
the manual source is the FTP path that will be used in practice (ZON-3;
see "FTP resolution" below and the documented precedence).

The run side has the same limitation (ZON-5/ZON-6): the owner has no run
threshold pace configured in Intervals.icu, so there is no real run-speed
stream and no real race effort either. Per section 12.4 the CS/D' fit is
validated on synthetic data with known parameters
(``tests/engine/test_zones_cs.py``) and VDOT against HAND-COMPUTED
reference anchors (``tests/engine/test_zones_vdot.py``); the run-threshold
side stays unexercised against real data until the owner fills in the
missing Intervals.icu field.

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

FTP resolution (ZON-3, PROJECT_BRIEF section 7.3)
-------------------------------------------------
FTP is configurable from three sources and the output ALWAYS states which
one is in use (a stable machine-readable ``source`` key plus a human
explanation in ``FtpResolution.detail``):

- ``"manual"``: a manual FTP value configured by the athlete. This is the
  path used in practice for this owner — there is no power meter, and the
  FTP of record is the manually confirmed 180 W (human-in-the-loop,
  section 7.3).
- ``"cp_derived"``: the CP of a :class:`CriticalPowerFit` times an explicit,
  configurable factor (default 1.0). The linear-form CP approximates FTP;
  taking CP directly as the FTP estimate (factor 1.0) is the standard
  convention AND a documented OWNER CHOICE here — the factor is a first
  parameter of :func:`resolve_ftp`, never buried.
- ``"twenty_min_power"``: 95% of the best 20-minute power (the 600 s point
  of the mean-maximal power curve). The 95% factor is the Allen & Coggan
  FTP convention (LITERATURE constant
  :data:`TWENTY_MIN_TO_FTP_FACTOR`).

When more than one source is available a documented, configurable
precedence decides (:data:`DEFAULT_FTP_PRECEDENCE`) and the result still
reports the winning source. Default precedence ``manual > cp_derived >
twenty_min_power`` is an OWNER CHOICE: the owner's manually confirmed value
is the FTP of record and always wins while configured. When no source is
available, :func:`resolve_ftp` raises ``ValueError`` naming all three.

Power zones (ZON-4, PROJECT_BRIEF sections 7.3 and 12.4)
--------------------------------------------------------
The Coggan power zone table, exactly as printed in section 7.3 (% of FTP):

    Z1 < 55, Z2 56-75, Z3 76-90, Z4 91-105, Z5 106-120, Z6 121-150, Z7 > 150

(LITERATURE: Allen & Coggan, "Training and Racing with a Power Meter";
zone names from the same source.) Absolute watt ranges are derived from
the FTP and reported unrounded (pct x FTP / 100); rounding for display is
a caller concern.

Gap rule for fractional percentages: the printed bounds leave gaps for
values strictly between whole numbers (e.g. 55.5%). The implemented rule
is the continuous partition at the printed boundary values themselves:
each of Z2-Z6 spans from its printed lower boundary value (inclusive) to
the next zone's boundary value (exclusive) — Z2 = [55, 76), Z3 = [76, 91),
Z4 = [91, 106), Z5 = [106, 121), Z6 = [121, 150] — while Z1 stays strictly
below 55 and Z7 strictly above 150, exactly as printed. So 55.0% and 55.5%
are Z2, 75.5% is still Z2, 76.0% is Z3, and 150.5% is Z7: both strict
printed inequalities are preserved and the fractional gaps join the zone
whose printed range they adjoin from below.

Critical Speed (ZON-5, PROJECT_BRIEF section 7.3)
-------------------------------------------------
The running mirror of Critical Power: from the mean-maximal SPEED curve,
take the best efforts between roughly 3 and 20 minutes and fit the linear
2-parameter model in its distance form:

    Distance(m) = CS(m/s) * t(s) + D'(m)

where ``Distance = v(t) * t`` for each curve point. CS (m/s) is the slope
— the theoretical speed that could be sustained indefinitely — and D' (m)
the finite distance capacity above CS. The fit is ordinary least squares
on the ``(t, Distance)`` points, using the SAME closed-form math as the
CP fit (a shared private helper; see ``fit_critical_speed``): the two
models are algebraically identical, only the axis changes (work vs
distance), so the implementation is GENERALISED, not duplicated. Fit
quality is reported as R^2 and RMSE (in metres, on the Distance axis)
plus the number of curve points used. The 3-20 min window (180-1200 s)
is the section 7.3 recommendation for running (the CP window shifted by
the longer relative duration of run efforts), documented in
:data:`CS_MIN_DURATION_S` / :data:`CS_MAX_DURATION_S`.

VDOT and training paces (ZON-6, PROJECT_BRIEF section 7.3)
----------------------------------------------------------
The Daniels & Gilbert VDOT model, formulas implemented VERBATIM from
section 7.3 (Daniels, "Daniels' Running Formula", Human Kinetics):

    VO2     = -4.60 + 0.182258 * v + 0.000104 * v^2      (v in m/min)
    %VO2max = 0.8 + 0.1894393 * e^(-0.012778 t)
              + 0.2989558 * e^(-0.1932605 t)              (t in min)
    VDOT    = VO2 / %VO2max

:func:`vdot_from_effort` converts a race/time-trial effort (distance,
duration) to VDOT; the hand-computed anchors in
``tests/engine/test_zones_vdot.py`` include 5000 m in 20:00 ->
VDOT = 49.806 (Daniels' published table says ~= 50).

Training paces: each of the five Daniels training types targets a fixed
percentage of VO2max, so its velocity is the velocity whose VO2 equals
``pct * VDOT`` — the POSITIVE root of the (quadratic) VO2 equation,
computed by :func:`velocity_from_vo2`. A target VO2 at or below zero has
no physically meaningful velocity and raises ``ValueError`` rather than
returning a negative or imaginary velocity. The Daniels intensity bands
(%VO2max, from the training-intensity table in "Daniels' Running Formula",
3rd ed., Human Kinetics 2013): Easy 59-74, Marathon 75-84, Threshold
83-88, Interval 95-100, Repetition 105-120. The DEFAULT pace point for
each band is its MIDPOINT — an OWNER-REVIEWABLE choice inside the
published band, exposed as an explicit keyword parameter of
:func:`training_paces` (:data:`DEFAULT_*_PCT_VO2MAX` constants), never
buried. Paces are reported as m/s (SI, feeds prediction section 7.7) AND
as pace in seconds per km (exact, sortable) with a ``min:sec /km`` label
(the display convention runners actually use); the label is derived, the
numeric fields are authoritative.

Swim Critical Swim Speed (ZON-7, PROJECT_BRIEF section 7.3)
-----------------------------------------------------------
The two-time-trial CSS model of Wakayoshi et al. 1992 ("Relationships
between swimming performance, body size and aerobic/anaerobic power"),
implemented exactly as printed in section 7.3:

    CSS (m/s) = (400 - 200) / (T400 - T200)

where T400 and T200 are the 400 m and 200 m time-trial durations in seconds
(entered in the tool via WhatsApp or detected in pool sessions). The
validation requirement (``T400 > T200``) is the formula's own: the
denominator must be positive, and both times must be positive.
:func:`css_from_time_trials` returns a frozen :class:`CssResult` with CSS in
m/s, the pace per 100 m in exact seconds plus a ``min:sec /100 m`` display
label, and the inputs. Hand-computed anchors (also the test references):

- T400 = 360 s (6:00), T200 = 170 s (2:50): CSS = 200 / 190 = 1.052631... m/s
  -> 95.0 s per 100 m = 1:35 /100 m.
- T400 = 480 s (8:00), T200 = 240 s (4:00): CSS = 200 / 240 = 0.833333... m/s
  -> 120.0 s per 100 m = 2:00 /100 m — exactly the CSS pace the owner has
  configured in Intervals.icu (real-world consistency anchor).

Swim zones relative to CSS: the brief NAMES swim zones relative to CSS but
publishes NO percentage table (unlike the Coggan power and Friel HR tables),
so the implemented table is an explicit, documented OWNER-REVIEWABLE choice
(see :func:`swim_zones` and :data:`DEFAULT_SWIM_ZONE_BOUNDARY_PCTS`). The
single literature-anchored point is 100% CSS = Threshold: by the
critical-speed model CSS itself is the threshold intensity (Wakayoshi et al.
1992). Zones are reported as paces per 100 m, and the pace axis runs OPPOSITE
to the power/HR tables: a FASTER pace (fewer seconds per 100 m) is a HIGHER
intensity. The same continuous-partition gap rule as the power zones applies,
with every boundary pace joining the faster (higher-intensity) zone.

Friel heart-rate zones (ZON-8, PROJECT_BRIEF sections 7.3 and 12.4)
-------------------------------------------------------------------
The two published Friel tables in % of LTHR, implemented EXACTLY as printed,
as SEPARATE per-sport tables (they differ for Z1/Z2/Z3 — run 85/89/94 vs bike
81/89/93 — and the tests assert that difference so a copy-paste mistake
cannot pass):

    Run : Z1 < 85, Z2 85-89, Z3 90-94, Z4 95-99, Z5a 100-102, Z5b 103-106, Z5c > 106
    Bike: Z1 < 81, Z2 81-89, Z3 90-93, Z4 94-99, Z5a 100-102, Z5b 103-106, Z5c > 106

(LITERATURE: Friel, "The Triathlete's Training Bible" / section 7.3; zone
keys as printed — the zone NAME labels are an OWNER CHOICE, the brief
publishes only the keys and percentages.) Absolute bpm bounds are
pct x LTHR / 100, reported unrounded, for the athlete's per-sport LTHR (the
owner's run/bike LTHR of record is 169 bpm). The SAME documented
continuous-partition gap rule as the Coggan power zones resolves fractional
percentages: each zone spans from its printed lower boundary (inclusive) to
the next zone's boundary (exclusive); Z1 stays strictly below its first
printed boundary and Z5c strictly above its last, exactly as printed — so
Z2 = [85, 90) for run, Z5b = [103, 106] (the printed 106 stays in Z5b
because Z5c is strictly > 106), and 89.5% is Z2. A per-sport classifier
(:func:`hr_zone_for`) matches an absolute bpm against the same table; a
non-positive LTHR raises ``ValueError``.

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

Threshold change detection (ZON-9, PROJECT_BRIEF section 7.3)
-------------------------------------------------------------
When a new effort exceeds the current model value for a threshold metric by
MORE than a configurable relative margin, :func:`propose_threshold_change`
returns a typed PROPOSAL — never an applied update. The proposal
(:class:`ThresholdChangeProposal`) carries everything needed to justify it
(section 7.3: "proposal object carries evidence and prior value"): the
proposed value, the prior (current) value, the delta and relative delta,
the margin used, the caller-supplied effort evidence and the explicit
non-applied status marker ``"proposal_not_applied"``. When the margin is
NOT exceeded (no increase over the current value, or an increase at or
below the margin — the comparison is strict), the function returns an
explicit :class:`NoThresholdChange` outcome with a machine-readable reason
(``"no_improvement"`` / ``"margin_not_exceeded"``) — never ``None``
silently and never a raise.

The function is INCAPABLE of applying anything: it is pure (no I/O, no DB,
section 6 purity), its result types are frozen dataclasses with no
mutating capability, and the engine module defines no apply / commit /
persist entry point at all. The athlete-confirmed application and the
history write belong to the CALLER layer (ZON-10 persistence, with the
WhatsApp confirmation flow landing in Feature 6) — never here.

Metrics are the thresholds the model actually produces, with stable
machine-readable keys (:data:`THRESHOLD_METRIC_KEYS`): ``ftp_watts``,
``cp_watts``, ``cs_mps`` and ``css_mps``. All four are natively
"higher exceeds" quantities (watts, m/s), so the comparison runs in native
units. EXTENSIBILITY: a new metric is one new key in the
``ThresholdMetricKey`` Literal plus one entry in
:data:`THRESHOLD_METRIC_UNITS` (its display unit) — the comparison, margin
semantics and outcome types are metric-agnostic. (A future inverted-axis
metric such as a pace in s/km would need an explicit direction flag in the
registry; deliberately not invented until such a metric exists.)

Margin semantics: RELATIVE to the current value — a proposal requires
``relative_delta = (new - current) / current`` to be strictly greater than
the margin (default :data:`DEFAULT_THRESHOLD_CHANGE_MARGIN` = 0.05, an
OWNER CHOICE pending confirmation, mirrored in Settings as
``ENGINE_THRESHOLD_CHANGE_MARGIN``, ZON-11). The exact-boundary comparison
goes through :func:`_same_boundary` (the same tolerant rule as the HR zone
bounds), so float noise at the boundary cannot flip the outcome.

Configurable constants (ZON-11, §7/§14)
---------------------------------------
Every constant below is a function parameter whose default is the
documented module constant (the documented fallback). ZON-11 mirrored the
effective values into Settings (``app.core.settings`` fields ``engine_*``,
each with its source/owner-choice comment) and added the settings→engine
mapping helper ``app.db.zones_config.zone_constants_from_settings``
(following the LOAD-11 pattern of ``app.db.daily_load``). NOTHING in
``app/`` consumes zones yet (Feature 6 will wire the ``get_zones`` tool
and the proposal flow), so that helper deliberately has NO caller today.
This module still never imports settings (§6 purity):

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
- CP-to-FTP factor default 1.0 (:func:`resolve_ftp` parameter
  ``cp_to_ftp_factor``): the linear-form CP approximates FTP; taking CP
  directly as the FTP estimate is the standard convention and a documented
  OWNER CHOICE — explicitly configurable, never buried.
- FTP source precedence default ``manual > cp_derived > twenty_min_power``
  (:func:`resolve_ftp` parameter ``precedence``): OWNER CHOICE — the owner
  has no power meter and the manually confirmed 180 W is the FTP of record
  (human-in-the-loop, section 7.3).
- 20-min-to-FTP factor 0.95 (:data:`TWENTY_MIN_TO_FTP_FACTOR`) and the
  600 s reference duration (:data:`FTP_TWENTY_MIN_DURATION_S`):
  LITERATURE (Allen & Coggan FTP convention; section 7.3 fixes the 95%).
- Coggan power zone percentages (:data:`_COGGAN_ZONE_SPECS`): LITERATURE,
  exactly as printed in section 7.3 (section 12.4 acceptance); the
  fractional-gap rule is a documented implementation choice (see the
  "Power zones" section above).
- CS fit window bounds 3 min (180 s) and 20 min (1200 s): the published
  recommendation for the linear CS model (section 7.3 — "best efforts
  between roughly 3 and 20 minutes"); LITERATURE reference, not an owner
  choice; :func:`fit_critical_speed` parameters ``min_duration_s`` /
  ``max_duration_s``.
- MMS duration set :data:`DEFAULT_MMS_DURATIONS_S`: OWNER CHOICE — a
  fixed chart-friendly speed ladder; the 180-1200 s points feed the CS
  fit.
- Daniels intensity bands (%VO2max: Easy 59-74, Marathon 75-84,
  Threshold 83-88, Interval 95-100, Repetition 105-120): LITERATURE
  ("Daniels' Running Formula", 3rd ed., Human Kinetics 2013,
  training-intensity table); the default pace point inside each band is
  the band MIDPOINT — OWNER-REVIEWABLE choice, explicit per-call
  parameters of :func:`training_paces`
  (:data:`DEFAULT_EASY_PCT_VO2MAX` and siblings).
- Swim zone boundaries (% CSS speed: Recovery < 85, Aerobic 85-95,
  Tempo 95-100, Threshold 100-105, VO2 max >= 105,
  :data:`DEFAULT_SWIM_ZONE_BOUNDARY_PCTS`): the brief publishes no swim
  percentage table, so the five-band structure, zone names and ALL
  boundary percentages are an OWNER-REVIEWABLE choice — EXCEPT the 100%
  anchor, which is LITERATURE-grounded (critical speed IS the threshold
  intensity, Wakayoshi et al. 1992). Boundaries are per-call
  configurable (``boundary_pcts_css``).
- Friel heart-rate zone percentages (:data:`_RUN_FRIEL_ZONE_SPECS` /
  :data:`_BIKE_FRIEL_ZONE_SPECS`): LITERATURE, exactly as printed in
  section 7.3 (section 12.4 acceptance), as separate per-sport tables;
  the zone NAME labels are an OWNER CHOICE (the brief publishes only the
  keys and percentages); the fractional-gap rule is the same documented
  implementation choice as for the power zones.
"""

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "CP_MAX_DURATION_S",
    "CP_MIN_DURATION_S",
    "CS_MAX_DURATION_S",
    "CS_MIN_DURATION_S",
    "DEFAULT_CP_TO_FTP_FACTOR",
    "DEFAULT_EASY_PCT_VO2MAX",
    "DEFAULT_FTP_PRECEDENCE",
    "DEFAULT_INTERVAL_PCT_VO2MAX",
    "DEFAULT_MARATHON_PCT_VO2MAX",
    "DEFAULT_MMP_DURATIONS_S",
    "DEFAULT_MMS_DURATIONS_S",
    "DEFAULT_REPETITION_PCT_VO2MAX",
    "DEFAULT_SWIM_ZONE_BOUNDARY_PCTS",
    "DEFAULT_THRESHOLD_CHANGE_MARGIN",
    "DEFAULT_THRESHOLD_PCT_VO2MAX",
    "FTP_SOURCE_CP_DERIVED",
    "FTP_SOURCE_KEYS",
    "FTP_SOURCE_MANUAL",
    "FTP_SOURCE_TWENTY_MIN_POWER",
    "FTP_TWENTY_MIN_DURATION_S",
    "HR_SPORT_KEYS",
    "THRESHOLD_METRIC_KEYS",
    "THRESHOLD_METRIC_UNITS",
    "THRESHOLD_NO_PROPOSAL_REASONS",
    "TWENTY_MIN_TO_FTP_FACTOR",
    "CriticalPowerFit",
    "CriticalSpeedFit",
    "CssResult",
    "FtpResolution",
    "HrZone",
    "NoThresholdChange",
    "PowerZone",
    "SwimZone",
    "ThresholdChangeProposal",
    "TrainingPace",
    "VdotPaces",
    "css_from_time_trials",
    "fit_critical_power",
    "fit_critical_speed",
    "hr_zone_for",
    "hr_zones",
    "mean_maximal_power_curve",
    "mean_maximal_speed_curve",
    "percent_vo2max",
    "power_zone_for",
    "power_zones",
    "propose_threshold_change",
    "resolve_ftp",
    "swim_zone_for",
    "swim_zones",
    "training_paces",
    "vdot_from_effort",
    "velocity_from_vo2",
    "vo2_from_velocity",
]


CP_MIN_DURATION_S: Final[float] = 120.0
"""Lower bound of the CP fit window: 2 minutes (Jones et al. 2019).

LITERATURE constant (the 2-20 min window for the linear CP model);
mirrored in Settings as ``ENGINE_CP_FIT_WINDOW_MIN_S`` (ZON-11).
"""

CP_MAX_DURATION_S: Final[float] = 1200.0
"""Upper bound of the CP fit window: 20 minutes (Jones et al. 2019).

LITERATURE constant; mirrored in Settings as
``ENGINE_CP_FIT_WINDOW_MAX_S`` (ZON-11).
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
2-20 min points feed the CP fit. Mirrored in Settings as
``ENGINE_MMP_DURATIONS_S`` (comma-separated; ZON-11).
"""


def _best_mean_curve(
    samples_seq: Sequence[float | None],
    *,
    quantity: str,
    sample_interval_s: float,
    durations_s: Sequence[int] | None,
    min_valid_fraction: float,
) -> dict[int, float]:
    """Shared best-mean-per-duration extraction (power and speed curves).

    The mean-maximal POWER and mean-maximal SPEED curves are the same
    computation on different units, so the rolling-sum implementation is
    GENERALISED here and both public functions delegate (never duplicated).
    ``quantity`` ("power" / "speed") only parameterises the error messages.
    See :func:`mean_maximal_power_curve` for the documented gap rule and
    complexity note.
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
    samples = list(samples_seq)
    if not any(s is not None for s in samples):
        raise ValueError(
            f"no usable {quantity} data: samples sequence is empty or all missing"
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
    return _best_mean_curve(
        power_samples,
        quantity="power",
        sample_interval_s=sample_interval_s,
        durations_s=durations_s,
        min_valid_fraction=min_valid_fraction,
    )


DEFAULT_MMS_DURATIONS_S: Final[tuple[int, ...]] = (
    1,
    60,
    180,
    300,
    480,
    600,
    900,
    1200,
    1800,
    3600,
)
"""Default mean-maximal SPEED duration set in seconds: 1 s, 1, 3, 5, 8, 10,
15, 20, 30, 60 min.

OWNER CHOICE — a fixed chart-friendly ladder spanning sprint to hour pace;
the 180-1200 s points feed the CS fit (ZON-5), mirroring the bike ladder's
role for CP. Documented engine-side fallback only — NOT part of the ZON-11
mirrored settings set (Feature 6 may mirror it later if a consumer needs
it).
"""


def mean_maximal_speed_curve(
    speed_samples: Sequence[float | None],
    *,
    sample_interval_s: float = 1.0,
    durations_s: Sequence[int] | None = None,
    min_valid_fraction: float = 1.0,
) -> dict[int, float]:
    """Best mean speed over each requested duration (mean-maximal speed curve).

    The running mirror of :func:`mean_maximal_power_curve` (ZON-5): for
    each duration ``d`` the function scans every contiguous window of
    ``w = round(d / sample_interval_s)`` samples (rolling window, 1-sample
    step), keeps the windows that qualify under the gap rule, and reports
    the highest window mean speed in m/s. Durations with no qualifying
    window are absent from the result.

    ``speed_samples`` is the per-second ingest-parser sequence (``None``
    marks a stream gap). The gap/``min_valid_fraction`` rule, the
    "absent, never zero" convention, the validation errors and the
    O(k * n) complexity are IDENTICAL to the power curve — the
    implementation is generalised (shared private helper), not duplicated.
    ``durations_s`` defaults to :data:`DEFAULT_MMS_DURATIONS_S` (owner
    choice), ``min_valid_fraction`` to the strict 1.0 (owner choice).

    Reference: section 7.3 best-effort convention, same as the power curve
    (Allen & Coggan mean-maximal analysis applied to run speed).
    """
    return _best_mean_curve(
        speed_samples,
        quantity="speed",
        sample_interval_s=sample_interval_s,
        durations_s=durations_s,
        min_valid_fraction=min_valid_fraction,
    )


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


def _fit_linear_time_model(
    points: list[tuple[float, float]],
    *,
    subject: str,
    quantity: str,
    min_duration_s: float,
    max_duration_s: float,
) -> tuple[float, float, float, float, int]:
    """Shared closed-form OLS of ``y = slope * t + intercept`` (CP and CS fits).

    The Critical Power (Work = CP*t + W') and Critical Speed
    (Distance = CS*t + D') models are algebraically identical linear
    2-parameter models — only the axis changes (joules vs metres). The
    math is GENERALISED here once, and both public fits convert their
    curve to the (t, y) points and delegate; never duplicated. Returns
    ``(slope, intercept, r_squared, rmse, n_points)`` with R^2 and RMSE
    computed on the y axis. Validation matches :func:`fit_critical_power`.
    """
    if len(points) < 2:
        raise ValueError(
            f"not enough curve points to fit {subject}: need at least two "
            f"durations within the {min_duration_s!r}-{max_duration_s!r} s window, "
            f"got {len(points)}"
        )
    ts = [t for t, _ in points]
    ys = [y for _, y in points]
    n = float(len(points))
    sum_t = math.fsum(ts)
    sum_y = math.fsum(ys)
    sum_tt = math.fsum(t * t for t in ts)
    sum_ty = math.fsum(t * y for t, y in points)
    denominator = n * sum_tt - sum_t * sum_t
    if denominator == 0.0:  # pragma: no cover - unreachable via Mapping keys
        raise ValueError(
            "degenerate fit: all curve durations are identical, a 2-parameter "
            "line is undetermined"
        )
    slope = (n * sum_ty - sum_t * sum_y) / denominator
    intercept = (sum_y - slope * sum_t) / n

    mean_y = sum_y / n
    ss_res = math.fsum((y - (slope * t + intercept)) ** 2 for t, y in points)
    ss_tot = math.fsum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0.0:
        raise ValueError(
            f"degenerate curve: every point has the same {quantity}, "
            "R^2 is undefined"
        )
    rmse = math.sqrt(ss_res / n)
    return slope, intercept, 1.0 - ss_res / ss_tot, rmse, len(points)


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
    cp, w_prime, r_squared, rmse, n_points = _fit_linear_time_model(
        points,
        subject="Critical Power",
        quantity="work",
        min_duration_s=min_duration_s,
        max_duration_s=max_duration_s,
    )
    return CriticalPowerFit(
        cp_watts=cp,
        w_prime_joules=w_prime,
        r_squared=r_squared,
        rmse_joules=rmse,
        n_points=n_points,
    )


CS_MIN_DURATION_S: Final[float] = 180.0
"""Lower bound of the CS fit window: 3 minutes (section 7.3, running).

LITERATURE constant ("best efforts between roughly 3 and 20 minutes",
section 7.3); mirrored in Settings as ``ENGINE_CS_FIT_WINDOW_MIN_S``
(ZON-11).
"""

CS_MAX_DURATION_S: Final[float] = 1200.0
"""Upper bound of the CS fit window: 20 minutes (section 7.3, running).

LITERATURE constant; mirrored in Settings as ``ENGINE_CS_FIT_WINDOW_MAX_S``
(ZON-11).
"""


@dataclass(frozen=True, slots=True)
class CriticalSpeedFit:
    """Result of the linear Critical Speed fit on a mean-maximal speed curve.

    Running mirror of :class:`CriticalPowerFit` (ZON-5): ``cs_mps`` is the
    CS slope (m/s), ``d_prime_meters`` the D' curvature constant (m),
    ``r_squared`` and ``rmse_meters`` the fit quality on the Distance axis
    (RMSE in metres), and ``n_points`` the number of curve points inside
    the 3-20 min fit window that the fit used. Frozen and slotted,
    consistent with :class:`CriticalPowerFit` and the other engine result
    conventions.
    """

    cs_mps: float
    d_prime_meters: float
    r_squared: float
    rmse_meters: float
    n_points: int


def fit_critical_speed(
    curve: Mapping[int, float],
    *,
    min_duration_s: float = CS_MIN_DURATION_S,
    max_duration_s: float = CS_MAX_DURATION_S,
) -> CriticalSpeedFit:
    """Fit Distance = CS * t + D' to the 3-20 min best efforts of a speed curve.

    ZON-5 (section 7.3): curve points with
    ``min_duration_s <= duration <= max_duration_s`` (defaults
    :data:`CS_MIN_DURATION_S` / :data:`CS_MAX_DURATION_S`, the literature
    roughly 3-20 min window) are converted to distance
    ``Distance = speed * t`` and fitted by ordinary least squares in closed
    form — the SAME math as the CP fit (generalised into a shared private
    helper; the models are algebraically identical, only the axis changes):

        CS = (n * sum(t*D) - sum(t) * sum(D)) / (n * sum(t^2) - sum(t)^2)
        D' = (sum(D) - CS * sum(t)) / n

    Fit quality: R^2 = 1 - SS_res / SS_tot and RMSE = sqrt(SS_res / n),
    both computed on the Distance axis (metres), plus ``n_points``.

    Validation (``ValueError``, never silent): fewer than two curve points
    inside the window; ``max_duration_s <= min_duration_s`` or a
    non-positive window bound; and the degenerate all-equal-distance case
    (SS_tot = 0, where R^2 is mathematically undefined). Points outside
    the window are ignored — the linear model is only valid in the severe
    domain (Jones et al. 2019, running analogue).

    Hand-computed example (also the test reference):
        CS = 5.0 m/s, D' = 300 m, point at t = 240 s:
        Distance = 5.0 * 240 + 300 = 1500 m -> mean speed 1500/240 = 6.25 m/s.

    Reference: section 7.3 (Distance = CS x t + D', best efforts roughly
    3-20 min); the running mirror of Monod & Scherrer 1965 / Jones et al.
    2019.
    """
    if min_duration_s <= 0.0 or max_duration_s <= min_duration_s:
        raise ValueError(
            "fit window must satisfy 0 < min_duration_s < max_duration_s, got "
            f"min_duration_s={min_duration_s!r}, max_duration_s={max_duration_s!r}"
        )
    points = sorted(
        (float(duration), speed * duration)
        for duration, speed in curve.items()
        if min_duration_s <= duration <= max_duration_s
    )
    cs, d_prime, r_squared, rmse, n_points = _fit_linear_time_model(
        points,
        subject="Critical Speed",
        quantity="distance",
        min_duration_s=min_duration_s,
        max_duration_s=max_duration_s,
    )
    return CriticalSpeedFit(
        cs_mps=cs,
        d_prime_meters=d_prime,
        r_squared=r_squared,
        rmse_meters=rmse,
        n_points=n_points,
    )


FtpSourceKey = Literal["manual", "cp_derived", "twenty_min_power"]
"""Stable machine-readable keys of the three FTP sources (section 7.3)."""

FTP_SOURCE_MANUAL: Final[FtpSourceKey] = "manual"
"""Machine-readable source key: a manual FTP value configured by the athlete.

The path used in PRACTICE for this owner (no power meter; the FTP of record
is the manually confirmed 180 W) — see the module docstring, "FTP resolution".
"""

FTP_SOURCE_CP_DERIVED: Final[FtpSourceKey] = "cp_derived"
"""Machine-readable source key: CP-derived estimate (CP x cp_to_ftp_factor)."""

FTP_SOURCE_TWENTY_MIN_POWER: Final[FtpSourceKey] = "twenty_min_power"
"""Machine-readable source key: 95% of best 20-minute power."""

FTP_SOURCE_KEYS: Final[tuple[FtpSourceKey, ...]] = (
    FTP_SOURCE_MANUAL,
    FTP_SOURCE_CP_DERIVED,
    FTP_SOURCE_TWENTY_MIN_POWER,
)
"""All three FTP source keys, in the default precedence order."""

DEFAULT_FTP_PRECEDENCE: Final[tuple[FtpSourceKey, ...]] = FTP_SOURCE_KEYS
"""Default FTP source precedence: manual > cp_derived > twenty_min_power.

OWNER CHOICE: the owner has no power meter and the manually confirmed 180 W
is the FTP of record (human-in-the-loop, section 7.3), so the manual source
always wins while configured. Configurable per call via ``resolve_ftp``.
"""

DEFAULT_CP_TO_FTP_FACTOR: Final[float] = 1.0
"""Default CP-to-FTP factor: the linear-form CP approximates FTP.

STANDARD CONVENTION and documented OWNER CHOICE: CP (the linear-form slope)
is taken directly as the FTP estimate. Explicitly configurable per call
(``resolve_ftp`` parameter ``cp_to_ftp_factor``) — deliberately visible as a
parameter, never buried in the arithmetic.
"""

FTP_TWENTY_MIN_DURATION_S: Final[int] = 600
"""Duration of the mean-maximal power curve point used for the FTP estimate:
20 minutes = 600 s. LITERATURE (Allen & Coggan)."""

TWENTY_MIN_TO_FTP_FACTOR: Final[float] = 0.95
"""FTP = 95% of best 20-minute power. LITERATURE (Allen & Coggan FTP
convention; section 7.3 fixes the 95%). An explicit parameter of
:func:`resolve_ftp` and mirrored in Settings as
``ENGINE_TWENTY_MIN_TO_FTP_FACTOR`` (ZON-11)."""


@dataclass(frozen=True, slots=True)
class FtpResolution:
    """Resolved FTP that always states which source is in use (section 7.3).

    ``ftp_watts`` is the resolved FTP, ``source`` the stable machine-readable
    key of the winning source (:data:`FTP_SOURCE_KEYS`) and ``detail`` a
    human-readable explanation of the choice (including the arithmetic for
    the derived sources). ``available_sources`` lists every source that had
    a value, in precedence order. The remaining fields carry the winning
    source's provenance: ``cp_watts`` / ``cp_to_ftp_factor`` for
    ``"cp_derived"`` and ``best_20_min_power_watts`` for
    ``"twenty_min_power"`` (``None`` otherwise). Frozen and slotted,
    consistent with :class:`CriticalPowerFit` and the other engine result
    conventions.
    """

    ftp_watts: float
    source: FtpSourceKey
    detail: str
    available_sources: tuple[FtpSourceKey, ...] = ()
    cp_watts: float | None = None
    cp_to_ftp_factor: float | None = None
    best_20_min_power_watts: float | None = None


def resolve_ftp(
    *,
    manual_ftp_watts: float | None = None,
    cp_fit: CriticalPowerFit | None = None,
    cp_to_ftp_factor: float = DEFAULT_CP_TO_FTP_FACTOR,
    curve: Mapping[int, float] | None = None,
    twenty_min_power_watts: float | None = None,
    precedence: Sequence[FtpSourceKey] = DEFAULT_FTP_PRECEDENCE,
    twenty_min_to_ftp_factor: float = TWENTY_MIN_TO_FTP_FACTOR,
) -> FtpResolution:
    """Resolve FTP from up to three configurable sources, stating the source.

    Sources (any combination; each contributes a candidate value):

    - ``manual_ftp_watts``: the athlete's manually configured FTP (the
      PRACTICAL path for this owner — no power meter, FTP of record is the
      manually confirmed 180 W). Must be positive.
    - ``cp_fit``: a :class:`CriticalPowerFit`; the candidate is
      ``cp_fit.cp_watts * cp_to_ftp_factor``. The linear-form CP
      approximates FTP; the factor default 1.0 is the standard convention
      AND a documented OWNER CHOICE, kept explicit as a parameter (never
      buried). CP and the factor must be positive.
    - best 20-minute power: ``twenty_min_power_watts`` directly, or the
      :data:`FTP_TWENTY_MIN_DURATION_S` (600 s) point of ``curve`` when the
      direct value is not given (a curve without a 600 s point leaves the
      source unavailable). The candidate is
      ``twenty_min_to_ftp_factor`` times that power (default 0.95,
      :data:`TWENTY_MIN_TO_FTP_FACTOR`, LITERATURE — ZON-11 mirrors it in
      Settings as ``ENGINE_TWENTY_MIN_TO_FTP_FACTOR``). The value must be
      positive.

    When several sources are available, the first one in ``precedence`` (a
    permutation of :data:`FTP_SOURCE_KEYS`; default :data:`DEFAULT_FTP_PRECEDENCE`,
    manual first — OWNER CHOICE) wins, and the returned
    :class:`FtpResolution` still reports the winning ``source`` key. With no
    source available, raises ``ValueError`` naming all three. Every
    configured value must be positive (``ValueError``), even one that would
    lose the precedence.

    Returns a frozen :class:`FtpResolution` whose ``source`` is always
    stated (machine-readable key plus human explanation in ``detail``).
    """
    order = tuple(precedence)
    if sorted(order) != sorted(FTP_SOURCE_KEYS):
        raise ValueError(
            "precedence must be a permutation of the three FTP source keys "
            f"{FTP_SOURCE_KEYS}, got {order!r}"
        )
    if cp_to_ftp_factor <= 0.0:
        raise ValueError(
            f"cp_to_ftp_factor must be positive, got {cp_to_ftp_factor!r}"
        )
    if twenty_min_to_ftp_factor <= 0.0:
        raise ValueError(
            "twenty_min_to_ftp_factor must be positive, got "
            f"{twenty_min_to_ftp_factor!r}"
        )

    candidates: dict[FtpSourceKey, float] = {}
    if manual_ftp_watts is not None:
        if manual_ftp_watts <= 0.0:
            raise ValueError(
                f"manual FTP value must be positive, got {manual_ftp_watts!r}"
            )
        candidates[FTP_SOURCE_MANUAL] = manual_ftp_watts
    if cp_fit is not None:
        if cp_fit.cp_watts <= 0.0:
            raise ValueError(
                "CP-derived FTP is not usable: CP must be positive, got "
                f"{cp_fit.cp_watts!r} W"
            )
        candidates[FTP_SOURCE_CP_DERIVED] = cp_fit.cp_watts * cp_to_ftp_factor
    twenty_min = twenty_min_power_watts
    if twenty_min is None and curve is not None:
        twenty_min = curve.get(FTP_TWENTY_MIN_DURATION_S)
    if twenty_min is not None:
        if twenty_min <= 0.0:
            raise ValueError(
                "best 20-minute power must be positive, got "
                f"{twenty_min!r} W (source {FTP_SOURCE_TWENTY_MIN_POWER!r})"
            )
        candidates[FTP_SOURCE_TWENTY_MIN_POWER] = (
            twenty_min_to_ftp_factor * twenty_min
        )

    if not candidates:
        raise ValueError(
            "no FTP source available: provide a manual FTP value, a "
            "CriticalPowerFit (CP-derived estimate) or a best 20-minute "
            f"power; all three sources ({', '.join(FTP_SOURCE_KEYS)}) are missing"
        )

    source = next(s for s in order if s in candidates)
    ftp_watts = candidates[source]
    if source == FTP_SOURCE_MANUAL:
        detail = (
            f"manual FTP value of {ftp_watts:.1f} W supplied by the athlete "
            "(owner-confirmed FTP of record; no power meter)"
        )
        provenance: dict[str, float | None] = {
            "cp_watts": None,
            "cp_to_ftp_factor": None,
            "best_20_min_power_watts": None,
        }
    elif source == FTP_SOURCE_CP_DERIVED:
        assert cp_fit is not None  # the candidate could only come from cp_fit
        detail = (
            f"CP-derived FTP estimate: CP {cp_fit.cp_watts:.1f} W x "
            f"cp_to_ftp_factor {cp_to_ftp_factor:g} = {ftp_watts:.1f} W "
            "(the linear-form CP approximates FTP; factor 1.0 is the "
            "documented owner choice / standard convention)"
        )
        provenance = {
            "cp_watts": cp_fit.cp_watts,
            "cp_to_ftp_factor": cp_to_ftp_factor,
            "best_20_min_power_watts": None,
        }
    else:
        assert twenty_min is not None  # the candidate could only come from here
        detail = (
            f"{twenty_min_to_ftp_factor:g} x best 20-minute power "
            f"({FTP_TWENTY_MIN_DURATION_S} s MMP point) {twenty_min:.1f} W = "
            f"{ftp_watts:.2f} W FTP (Allen & Coggan FTP convention)"
        )
        provenance = {
            "cp_watts": None,
            "cp_to_ftp_factor": None,
            "best_20_min_power_watts": twenty_min,
        }
    return FtpResolution(
        ftp_watts=ftp_watts,
        source=source,
        detail=detail,
        available_sources=tuple(s for s in order if s in candidates),
        cp_watts=provenance["cp_watts"],
        cp_to_ftp_factor=provenance["cp_to_ftp_factor"],
        best_20_min_power_watts=provenance["best_20_min_power_watts"],
    )


@dataclass(frozen=True, slots=True)
class PowerZone:
    """One Coggan power zone, printed percentages plus absolute watt bounds.

    ``min_pct_ftp`` / ``max_pct_ftp`` are the EFFECTIVE percentage bounds of
    the zone under the documented gap rule (see the module docstring,
    "Power zones"); ``*_inclusive`` state whether each bound belongs to the
    zone (Z1's 55% top edge and Z7's 150% bottom edge are exclusive, exactly
    as printed: Z1 < 55, Z7 > 150). ``min_watts`` / ``max_watts`` are the
    same bounds in watts for the FTP the table was built from (pct x FTP /
    100, unrounded). ``None`` means unbounded on that side. Frozen and
    slotted, consistent with the other engine result conventions.
    """

    key: str
    name: str
    min_pct_ftp: float | None
    min_pct_inclusive: bool
    max_pct_ftp: float | None
    max_pct_inclusive: bool
    min_watts: float | None
    max_watts: float | None

    def contains_pct(self, pct: float) -> bool:
        """Whether a %FTP value falls inside this zone under the gap rule."""
        below_min = self.min_pct_ftp is not None and (
            pct < self.min_pct_ftp
            or (not self.min_pct_inclusive and pct == self.min_pct_ftp)
        )
        if below_min:
            return False
        above_max = self.max_pct_ftp is not None and (
            pct > self.max_pct_ftp
            or (not self.max_pct_inclusive and pct == self.max_pct_ftp)
        )
        return not above_max


# The Coggan power zone table, exactly as printed in section 7.3 (% of FTP):
# Z1 < 55, Z2 56-75, Z3 76-90, Z4 91-105, Z5 106-120, Z6 121-150, Z7 > 150.
# Effective bounds under the documented gap rule: Z2-Z6 span from the printed
# lower boundary value (inclusive) to the next zone's boundary value
# (exclusive); Z1 stays strictly below 55 and Z7 strictly above 150.
# (key, name, min_pct, min_inclusive, max_pct, max_inclusive)
# LITERATURE: Allen & Coggan, "Training and Racing with a Power Meter".
_COGGAN_ZONE_SPECS: Final[
    tuple[tuple[str, str, float | None, bool, float | None, bool], ...]
] = (
    ("Z1", "Active recovery", None, False, 55.0, False),
    ("Z2", "Endurance", 55.0, True, 76.0, False),
    ("Z3", "Tempo", 76.0, True, 91.0, False),
    ("Z4", "Threshold", 91.0, True, 106.0, False),
    ("Z5", "VO2 max", 106.0, True, 121.0, False),
    ("Z6", "Anaerobic capacity", 121.0, True, 150.0, True),
    ("Z7", "Neuromuscular power", 150.0, False, None, False),
)


def power_zones(ftp_watts: float) -> tuple[PowerZone, ...]:
    """The Coggan power zone table with absolute watt ranges for ``ftp_watts``.

    The published percentages are reproduced EXACTLY (section 7.3, section
    12.4 acceptance): Z1 < 55, Z2 56-75, Z3 76-90, Z4 91-105, Z5 106-120,
    Z6 121-150, Z7 > 150 (% FTP; Allen & Coggan). Absolute watt bounds are
    pct x FTP / 100, reported unrounded.

    Gap rule for fractional percentages (documented, not left undefined):
    the printed bounds leave gaps for values strictly between whole numbers
    (e.g. 55.5%). The implemented rule partitions the percentage space at
    the printed boundary values themselves: Z2 = [55, 76), Z3 = [76, 91),
    Z4 = [91, 106), Z5 = [106, 121), Z6 = [121, 150], Z1 strictly below 55,
    Z7 strictly above 150. So 55.0% and 55.5% are Z2, 75.5% is still Z2,
    76.0% is Z3, and 150.5% is Z7 — both strict printed inequalities
    (Z1 < 55, Z7 > 150) are preserved and each fractional gap joins the
    zone whose printed range it adjoins from below.

    ``ftp_watts`` must be positive (``ValueError``).
    """
    if ftp_watts <= 0.0:
        raise ValueError(f"ftp_watts must be positive, got {ftp_watts!r}")
    return tuple(
        PowerZone(
            key=key,
            name=name,
            min_pct_ftp=min_pct,
            min_pct_inclusive=min_inclusive,
            max_pct_ftp=max_pct,
            max_pct_inclusive=max_inclusive,
            min_watts=None if min_pct is None else min_pct * ftp_watts / 100.0,
            max_watts=None if max_pct is None else max_pct * ftp_watts / 100.0,
        )
        for key, name, min_pct, min_inclusive, max_pct, max_inclusive in (
            _COGGAN_ZONE_SPECS
        )
    )


def power_zone_for(watts: float, ftp_watts: float) -> PowerZone:
    """Classify an absolute power into its Coggan zone for ``ftp_watts``.

    Consistent with :func:`power_zones`: the value is converted to % FTP
    (``watts / ftp_watts * 100``) and matched against the same effective
    bounds, including the documented fractional-gap rule (e.g. 55.5% of FTP
    is Z2, 150.5% is Z7 — see the :func:`power_zones` docstring).

    ``ftp_watts`` must be positive and ``watts`` non-negative
    (``ValueError``); zero watts is valid (coasting) and is Z1.
    """
    if ftp_watts <= 0.0:
        raise ValueError(f"ftp_watts must be positive, got {ftp_watts!r}")
    if watts < 0.0:
        raise ValueError(f"watts must not be negative, got {watts!r}")
    pct = watts * 100.0 / ftp_watts
    for zone in power_zones(ftp_watts):
        if zone.contains_pct(pct):
            return zone
    raise ValueError(  # pragma: no cover - the table covers [0, inf) exactly
        f"no power zone covers {watts!r} W at FTP {ftp_watts!r} "
        f"({pct!r}% FTP); this should be unreachable"
    )


# ---------------------------------------------------------------------------
# VDOT and Daniels training paces (ZON-6, PROJECT_BRIEF section 7.3)
# ---------------------------------------------------------------------------

VO2_INTERCEPT: Final[float] = -4.60
"""Intercept of the Daniels & Gilbert VO2-velocity equation (v in m/min).

LITERATURE constant, implemented verbatim from section 7.3.
"""

VO2_LINEAR_COEFF: Final[float] = 0.182258
"""Linear velocity coefficient of the Daniels & Gilbert VO2 equation.

LITERATURE constant, implemented verbatim from section 7.3.
"""

VO2_QUADRATIC_COEFF: Final[float] = 0.000104
"""Quadratic velocity coefficient of the Daniels & Gilbert VO2 equation.

LITERATURE constant, implemented verbatim from section 7.3.
"""

PCT_VO2MAX_ASYMPTOTE: Final[float] = 0.8
"""Asymptote of the Daniels & Gilbert %VO2max-vs-duration equation.

LITERATURE constant, implemented verbatim from section 7.3.
"""

PCT_VO2MAX_TERM1: Final[float] = 0.1894393
"""Amplitude of the first exponential in the %VO2max equation. LITERATURE."""

PCT_VO2MAX_DECAY1: Final[float] = 0.012778
"""Decay rate (per minute) of the first exponential in the %VO2max
equation. LITERATURE constant, implemented verbatim from section 7.3."""

PCT_VO2MAX_TERM2: Final[float] = 0.2989558
"""Amplitude of the second exponential in the %VO2max equation. LITERATURE."""

PCT_VO2MAX_DECAY2: Final[float] = 0.1932605
"""Decay rate (per minute) of the second exponential in the %VO2max
equation. LITERATURE constant, implemented verbatim from section 7.3."""


def vo2_from_velocity(velocity_m_per_min: float) -> float:
    """Running VO2 demand (ml/kg/min) at ``velocity_m_per_min`` (m/min).

    Daniels & Gilbert equation, verbatim (section 7.3):

        VO2 = -4.60 + 0.182258 * v + 0.000104 * v^2

    Hand-computed anchor: v = 250 m/min (5000 m in 20:00) ->
    VO2 = -4.60 + 45.5645 + 6.5 = 47.4645.

    ``velocity_m_per_min`` must be positive (``ValueError``): a zero or
    negative velocity is not a running effort.
    """
    if velocity_m_per_min <= 0.0:
        raise ValueError(
            f"velocity must be positive, got {velocity_m_per_min!r} m/min"
        )
    return (
        VO2_INTERCEPT
        + VO2_LINEAR_COEFF * velocity_m_per_min
        + VO2_QUADRATIC_COEFF * velocity_m_per_min**2
    )


def percent_vo2max(duration_min: float) -> float:
    """Fraction of VO2max sustainable for ``duration_min`` minutes (0..~1.1).

    Daniels & Gilbert equation, verbatim (section 7.3):

        %VO2max = 0.8 + 0.1894393 * e^(-0.012778 t)
                       + 0.2989558 * e^(-0.1932605 t)      (t in minutes)

    Hand-computed anchors: t = 20 min -> 0.952983 (so 20:00 for 5000 m
    needs ~95.3% of VO2max); t = 4 min -> 1.118000 (a 4-min race exceeds
    VO2max). The value declines toward the 0.8 asymptote as t grows.

    ``duration_min`` must be positive (``ValueError``).
    """
    if duration_min <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_min!r} minutes")
    return (
        PCT_VO2MAX_ASYMPTOTE
        + PCT_VO2MAX_TERM1 * math.exp(-PCT_VO2MAX_DECAY1 * duration_min)
        + PCT_VO2MAX_TERM2 * math.exp(-PCT_VO2MAX_DECAY2 * duration_min)
    )


def vdot_from_effort(distance_m: float, duration_s: float) -> float:
    """VDOT of a race or time-trial effort (Daniels & Gilbert, section 7.3).

    The effort is converted to a mean velocity ``v = distance_m / t_min``
    (m/min) and ``t_min = duration_s / 60`` minutes, then:

        VDOT = VO2(v) / %VO2max(t_min)

    with :func:`vo2_from_velocity` and :func:`percent_vo2max` (formulas
    verbatim from section 7.3).

    Hand-computed anchors (see ``tests/engine/test_zones_vdot.py`` for the
    full arithmetic):

    - 5000 m in 20:00 -> v = 250 m/min, VO2 = 47.4645,
      %VO2max = 0.952983 -> VDOT = 49.806 (Daniels' table ~= 50).
    - 10,000 m in 40:00 -> same v = 250 m/min -> VDOT = 51.944.
    - 1500 m in 4:00 -> v = 375 m/min -> VDOT = 70.100.

    ``distance_m`` and ``duration_s`` must both be positive
    (``ValueError``).
    """
    if distance_m <= 0.0:
        raise ValueError(f"distance must be positive, got {distance_m!r} metres")
    if duration_s <= 0.0:
        raise ValueError(f"duration must be positive, got {duration_s!r} seconds")
    t_min = duration_s / 60.0
    velocity_m_per_min = distance_m / t_min
    return vo2_from_velocity(velocity_m_per_min) / percent_vo2max(t_min)


def velocity_from_vo2(target_vo2: float) -> float:
    """Velocity (m/min) whose Daniels & Gilbert VO2 demand is ``target_vo2``.

    Inverts :func:`vo2_from_velocity`: the VO2 equation is quadratic in v,

        0.000104 * v^2 + 0.182258 * v - (4.60 + target) = 0

    so the velocity is the POSITIVE root (the negative root is
    physiological nonsense and is never returned):

        v = (-0.182258 + sqrt(0.182258^2 + 4 * 0.000104 * (4.60 + target)))
            / (2 * 0.000104)

    Hand check: target = VO2(250) = 47.4645 recovers v = 250 m/min.

    A target at or below zero has no physically meaningful velocity
    (VO2 demand of a running effort is positive) — raises ``ValueError``
    rather than returning a negative or imaginary velocity. The
    discriminant is positive for every positive target (guard kept as an
    explicit check).
    """
    if target_vo2 <= 0.0:
        raise ValueError(
            "target VO2 must be positive: a running velocity has a positive "
            f"oxygen demand, got {target_vo2!r}"
        )
    discriminant = VO2_LINEAR_COEFF**2 + 4.0 * VO2_QUADRATIC_COEFF * (
        -VO2_INTERCEPT + target_vo2
    )
    if discriminant < 0.0:  # pragma: no cover - unreachable for target > 0
        raise ValueError(
            f"target VO2 {target_vo2!r} yields no real velocity (negative "
            "discriminant); no physically meaningful pace exists"
        )
    return (
        -VO2_LINEAR_COEFF + math.sqrt(discriminant)
    ) / (2.0 * VO2_QUADRATIC_COEFF)


DEFAULT_EASY_PCT_VO2MAX: Final[float] = 66.5
"""Default Easy-pace intensity: midpoint of Daniels' 59-74% VO2max band.

OWNER-REVIEWABLE choice inside the published LITERATURE band (Daniels,
"Daniels' Running Formula", 3rd ed., Human Kinetics 2013,
training-intensity table); explicitly configurable per call via
:func:`training_paces` parameter ``easy_pct`` — never buried.
"""

DEFAULT_MARATHON_PCT_VO2MAX: Final[float] = 79.5
"""Default Marathon-pace intensity: midpoint of Daniels' 75-84% VO2max
band. OWNER-REVIEWABLE choice inside the published LITERATURE band;
configurable per call (``marathon_pct``)."""

DEFAULT_THRESHOLD_PCT_VO2MAX: Final[float] = 85.5
"""Default Threshold-pace intensity: midpoint of Daniels' 83-88% VO2max
band. OWNER-REVIEWABLE choice inside the published LITERATURE band;
configurable per call (``threshold_pct``)."""

DEFAULT_INTERVAL_PCT_VO2MAX: Final[float] = 97.5
"""Default Interval-pace intensity: midpoint of Daniels' 95-100% VO2max
band. OWNER-REVIEWABLE choice inside the published LITERATURE band;
configurable per call (``interval_pct``)."""

DEFAULT_REPETITION_PCT_VO2MAX: Final[float] = 112.5
"""Default Repetition-pace intensity: midpoint of Daniels' 105-120% VO2max
band (repetition work runs ABOVE VO2max, hence > 100%). OWNER-REVIEWABLE
choice inside the published LITERATURE band; configurable per call
(``repetition_pct``)."""


@dataclass(frozen=True, slots=True)
class TrainingPace:
    """One Daniels training pace derived from a VDOT (ZON-6).

    ``pct_vo2max`` is the intensity band point used (the OWNER-REVIEWABLE
    choice inside Daniels' published band), ``speed_mps`` the velocity in
    m/s (SI; feeds prediction, section 7.7), ``pace_sec_per_km`` the exact
    pace in seconds per km (numeric, sortable — the authoritative fields)
    and ``pace_per_km`` a derived ``min:sec /km`` display label (the
    convention runners actually use). Frozen and slotted, consistent with
    the other engine result conventions.
    """

    key: str
    name: str
    pct_vo2max: float
    speed_mps: float
    pace_sec_per_km: float
    pace_per_km: str


@dataclass(frozen=True, slots=True)
class VdotPaces:
    """The five Daniels training paces for one VDOT value (ZON-6).

    Fields ``easy``, ``marathon``, ``threshold``, ``interval`` and
    ``repetition`` are :class:`TrainingPace` results in ascending speed
    order; ``vdot`` carries the input VDOT the paces were derived from.
    Frozen and slotted.
    """

    vdot: float
    easy: TrainingPace
    marathon: TrainingPace
    threshold: TrainingPace
    interval: TrainingPace
    repetition: TrainingPace


def _training_pace(
    key: str, name: str, pct_vo2max: float, vdot: float
) -> TrainingPace:
    """Derive one training pace: positive root at ``pct/100 * VDOT`` VO2."""
    target_vo2 = pct_vo2max / 100.0 * vdot
    velocity_m_per_min = velocity_from_vo2(target_vo2)
    speed_mps = velocity_m_per_min / 60.0
    pace_sec_per_km = 1000.0 / speed_mps
    label_seconds = round(pace_sec_per_km)  # whole-second display label
    label = f"{label_seconds // 60}:{label_seconds % 60:02d} /km"
    return TrainingPace(
        key=key,
        name=name,
        pct_vo2max=pct_vo2max,
        speed_mps=speed_mps,
        pace_sec_per_km=pace_sec_per_km,
        pace_per_km=label,
    )


def training_paces(
    vdot: float,
    *,
    easy_pct: float = DEFAULT_EASY_PCT_VO2MAX,
    marathon_pct: float = DEFAULT_MARATHON_PCT_VO2MAX,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT_VO2MAX,
    interval_pct: float = DEFAULT_INTERVAL_PCT_VO2MAX,
    repetition_pct: float = DEFAULT_REPETITION_PCT_VO2MAX,
) -> VdotPaces:
    """Derive the five Daniels training paces for ``vdot`` (section 7.3).

    Each pace targets a fixed percentage of VO2max (the Daniels intensity
    band, "Daniels' Running Formula", 3rd ed., Human Kinetics 2013):
    Easy 59-74%, Marathon 75-84%, Threshold 83-88%, Interval 95-100%,
    Repetition 105-120% of VO2max. The DEFAULT pace point for each band is
    its midpoint (``easy_pct`` 66.5, ``marathon_pct`` 79.5,
    ``threshold_pct`` 85.5, ``interval_pct`` 97.5, ``repetition_pct``
    112.5) — each is an explicit OWNER-REVIEWABLE parameter, never buried.
    The velocity is :func:`velocity_from_vo2` at ``pct/100 * VDOT`` — the
    POSITIVE root of the VO2 quadratic.

    Hand-computed cross-check at VDOT = 50 with the defaults: threshold
    target = 0.855 * 50 = 42.75 -> v = 229.6917 m/min = 3.8282 m/s =
    261.22 s/km = 4:21.2 /km, matching Daniels' published VDOT-50
    threshold pace of ~4:21 /km.

    Paces are reported both as m/s and as seconds per km (with a derived
    ``min:sec /km`` label) — see :class:`TrainingPace`.

    ``vdot`` must be positive and every band percentage must produce a
    positive target VO2 (``ValueError``): a target at or below zero has no
    physically meaningful velocity, and the function raises instead of
    returning a negative or imaginary velocity.
    """
    if vdot <= 0.0:
        raise ValueError(f"vdot must be positive, got {vdot!r}")
    specs: Final[tuple[tuple[str, str, float], ...]] = (
        ("easy", "Easy", easy_pct),
        ("marathon", "Marathon", marathon_pct),
        ("threshold", "Threshold", threshold_pct),
        ("interval", "Interval", interval_pct),
        ("repetition", "Repetition", repetition_pct),
    )
    for _, _, pct in specs:
        if pct <= 0.0:
            raise ValueError(
                "band percentage must be positive: the target VO2 "
                f"(pct/100 * vdot = {pct / 100.0 * vdot:.4f}) would have no "
                "physically meaningful velocity"
            )
    paces = [_training_pace(key, name, pct, vdot) for key, name, pct in specs]
    return VdotPaces(
        vdot=vdot,
        easy=paces[0],
        marathon=paces[1],
        threshold=paces[2],
        interval=paces[3],
        repetition=paces[4],
    )



# ---------------------------------------------------------------------------
# Swim Critical Swim Speed and swim zones (ZON-7, PROJECT_BRIEF section 7.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CssResult:
    """Result of the two-time-trial CSS calculation (ZON-7, section 7.3).

    ``css_mps`` is the Critical Swim Speed in m/s (Wakayoshi et al. 1992),
    ``pace_sec_per_100m`` the exact pace per 100 m in seconds (the
    authoritative numeric field, sortable) and ``pace_per_100m`` a derived
    ``min:sec /100 m`` display label (the convention swimmers actually use).
    ``t400_s`` / ``t200_s`` echo the inputs the result was computed from.
    Frozen and slotted, consistent with the other engine result conventions.
    """

    css_mps: float
    t400_s: float
    t200_s: float
    pace_sec_per_100m: float
    pace_per_100m: str


def css_from_time_trials(t400_s: float, t200_s: float) -> CssResult:
    """Critical Swim Speed from 400 m and 200 m time trials (ZON-7, section 7.3).

    The two-time-trial CSS model (Wakayoshi et al. 1992), exactly as printed
    in section 7.3:

        CSS (m/s) = (400 - 200) / (T400 - T200)

    where T400 and T200 are the time-trial durations in seconds. The pace per
    100 m is ``100 / CSS`` seconds (exact, plus a derived ``min:sec /100 m``
    display label).

    Hand-computed anchors (also the test references):

    - T400 = 360 s (6:00), T200 = 170 s (2:50):
          CSS = (400 - 200) / (360 - 170) = 200 / 190 = 1.052631... m/s
          pace per 100 m = 100 * 190 / 200 = 95.0 s = 1:35 /100 m
    - T400 = 480 s (8:00), T200 = 240 s (4:00):
          CSS = (400 - 200) / (480 - 240) = 200 / 240 = 0.833333... m/s
          pace per 100 m = 100 * 240 / 200 = 120.0 s = 2:00 /100 m
          (exactly the CSS pace the owner has configured in Intervals.icu —
          a real-world consistency anchor)

    Validation (``ValueError``, never silent): both times must be positive
    and ``T400 > T200`` — the formula's denominator ``(T400 - T200)`` must be
    positive for a meaningful speed; equal or inverted times raise.

    Reference: Wakayoshi et al. 1992 (CSS from 400/200 m time trials);
    PROJECT_BRIEF section 7.3.
    """
    if t400_s <= 0.0 or t200_s <= 0.0:
        raise ValueError(
            "both time-trial durations must be positive, got "
            f"T400={t400_s!r} s, T200={t200_s!r} s"
        )
    if t400_s <= t200_s:
        raise ValueError(
            "T400 must be greater than T200 (the CSS denominator "
            f"T400 - T200 = {t400_s - t200_s!r} s must be positive), got "
            f"T400={t400_s!r} s, T200={t200_s!r} s"
        )
    css_mps = 200.0 / (t400_s - t200_s)
    pace_sec_per_100m = 100.0 / css_mps
    label_seconds = round(pace_sec_per_100m)  # whole-second display label
    label = f"{label_seconds // 60}:{label_seconds % 60:02d} /100 m"
    return CssResult(
        css_mps=css_mps,
        t400_s=t400_s,
        t200_s=t200_s,
        pace_sec_per_100m=pace_sec_per_100m,
        pace_per_100m=label,
    )


DEFAULT_SWIM_ZONE_BOUNDARY_PCTS: Final[tuple[float, float, float, float]] = (
    85.0,
    95.0,
    100.0,
    105.0,
)
"""Swim-zone boundaries in % of CSS SPEED: Recovery < 85, Aerobic 85-95,
Tempo 95-100, Threshold 100-105, VO2 max >= 105.

OWNER-REVIEWABLE CHOICE: PROJECT_BRIEF section 7.3 names swim zones relative
to CSS but publishes NO percentage table (unlike the Coggan power and Friel
HR tables), so the five-band structure, zone names and these boundary
percentages are the owner's documented choice — EXCEPT the 100% anchor,
which is LITERATURE-grounded (critical speed IS the threshold intensity,
Wakayoshi et al. 1992). Configurable per call via ``swim_zones`` /
``swim_zone_for`` parameter ``boundary_pcts_css`` and mirrored in Settings
as ``ENGINE_SWIM_ZONE_BOUNDARY_PCTS`` (comma-separated; ZON-11).
"""

_SWIM_ZONE_KEYS: Final[tuple[str, ...]] = ("Z1", "Z2", "Z3", "Z4", "Z5")
"""Swim zone keys, in ascending intensity order (OWNER CHOICE — the brief
publishes no swim zone table)."""

_SWIM_ZONE_NAMES: Final[tuple[str, ...]] = (
    "Recovery",
    "Aerobic",
    "Tempo",
    "Threshold",
    "VO2 max",
)
"""Swim zone names, in ascending intensity order (OWNER CHOICE)."""


@dataclass(frozen=True, slots=True)
class SwimZone:
    """One swim zone around CSS, in % CSS speed AND pace per 100 m (ZON-7).

    ``min_pct_css`` / ``max_pct_css`` are the EFFECTIVE speed-percentage
    bounds of the zone (fraction of CSS speed) under the documented
    continuous-partition rule; ``*_inclusive`` state whether each bound
    belongs to the zone (Z1's upper edge is exclusive — strictly below the
    first boundary — and Z5's lower edge is inclusive: each boundary value
    joins the higher-intensity zone adjoining it).

    ``min_pace_sec_per_100m`` / ``max_pace_sec_per_100m`` are the SAME bounds
    on the pace-per-100 m axis for the CSS the table was built from. The pace
    axis INVERTS the speed axis: a faster pace (FEWER seconds per 100 m) is a
    HIGHER intensity — the opposite direction from the power (watts) and
    heart-rate (bpm) tables. So a zone's slower pace edge (larger seconds,
    ``max_pace_sec_per_100m``) mirrors its speed minimum and its faster pace
    edge (``min_pace_sec_per_100m``) mirrors its speed maximum, with the
    inclusivity flags inverted accordingly. ``None`` means unbounded on that
    side. Frozen and slotted, consistent with :class:`PowerZone` and the
    other engine result conventions.
    """

    key: str
    name: str
    min_pct_css: float | None
    min_pct_inclusive: bool
    max_pct_css: float | None
    max_pct_inclusive: bool
    min_pace_sec_per_100m: float | None
    min_pace_inclusive: bool
    max_pace_sec_per_100m: float | None
    max_pace_inclusive: bool

    def contains_pct(self, pct: float) -> bool:
        """Whether a % CSS speed value falls inside this zone (speed axis)."""
        below_min = self.min_pct_css is not None and (
            pct < self.min_pct_css
            or (not self.min_pct_inclusive and pct == self.min_pct_css)
        )
        if below_min:
            return False
        above_max = self.max_pct_css is not None and (
            pct > self.max_pct_css
            or (not self.max_pct_inclusive and pct == self.max_pct_css)
        )
        return not above_max

    def contains_pace(self, pace_sec_per_100m: float) -> bool:
        """Whether a pace per 100 m falls inside this zone (pace axis).

        The pace axis is the speed axis inverted (faster pace = higher
        intensity), so the bounds and inclusivity flags are mirrored: this is
        the comparison :func:`swim_zone_for` uses, and it is exact for the
        table's own boundary paces (no percentage round-trip).
        """
        too_slow = self.max_pace_sec_per_100m is not None and (
            pace_sec_per_100m > self.max_pace_sec_per_100m
            or (
                not self.max_pace_inclusive
                and pace_sec_per_100m == self.max_pace_sec_per_100m
            )
        )
        if too_slow:
            return False
        too_fast = self.min_pace_sec_per_100m is not None and (
            pace_sec_per_100m < self.min_pace_sec_per_100m
            or (
                not self.min_pace_inclusive
                and pace_sec_per_100m == self.min_pace_sec_per_100m
            )
        )
        return not too_fast


def _swim_pace_at_pct(pct: float, css_mps: float) -> float:
    """Pace per 100 m (s) at ``pct`` % of CSS speed (the pace-axis mapping)."""
    return 100.0 / (pct / 100.0 * css_mps)


def swim_zones(
    css_mps: float,
    *,
    boundary_pcts_css: tuple[float, float, float, float] = (
        DEFAULT_SWIM_ZONE_BOUNDARY_PCTS
    ),
) -> tuple[SwimZone, ...]:
    """The swim zone table (paces per 100 m) for a CSS of ``css_mps`` (ZON-7).

    The brief (section 7.3) names swim zones relative to CSS but publishes NO
    percentage table (unlike Coggan power / Friel HR), so this table is an
    explicit, documented OWNER-REVIEWABLE choice — see
    :data:`DEFAULT_SWIM_ZONE_BOUNDARY_PCTS`. Default bands in % of CSS speed:

        Z1 Recovery  < 85     Z4 Threshold  100-105
        Z2 Aerobic   85-95    Z5 VO2 max    >= 105
        Z3 Tempo     95-100

    The single LITERATURE-anchored point is 100% CSS = Threshold: by the
    critical-speed model, CSS itself is the threshold intensity (Wakayoshi et
    al. 1992). The remaining boundaries and the band structure are owner
    choices, configurable per call via ``boundary_pcts_css`` (four strictly
    increasing positive boundaries defining five zones).

    DIRECTION NOTE: zones are reported as paces per 100 m, and a FASTER pace
    (fewer seconds per 100 m) is a HIGHER intensity — the opposite direction
    from the power (watts) and heart-rate (bpm) tables. Each zone's pace
    bounds mirror its speed bounds with inclusivity inverted
    (:class:`SwimZone`).

    Gap rule (same continuous-partition rule as the Coggan power zones): the
    zones partition the percentage space at the boundary values themselves,
    each zone spanning from its lower boundary (inclusive) to the next
    boundary (exclusive). On the pace axis every boundary pace therefore
    joins the FASTER (higher-intensity) zone adjoining it — e.g. at the
    anchor-(a) CSS the boundary pace 100.0 s /100 m (exactly 95% CSS speed)
    is Z3, and the CSS pace itself (100% CSS) is Z4 Threshold.

    ``css_mps`` must be positive (``ValueError``); an invalid boundary tuple
    (not exactly four strictly increasing positive values) raises
    ``ValueError``.
    """
    if css_mps <= 0.0:
        raise ValueError(f"css_mps must be positive, got {css_mps!r}")
    boundaries = tuple(boundary_pcts_css)
    if len(boundaries) != 4:
        raise ValueError(
            "boundary_pcts_css must be exactly four strictly increasing "
            f"positive boundaries defining five zones, got {boundaries!r}"
        )
    if any(b <= 0.0 for b in boundaries) or not all(
        lo < hi for lo, hi in itertools.pairwise(boundaries)
    ):
        raise ValueError(
            "boundary_pcts_css must be strictly increasing and positive, got "
            f"{boundaries!r}"
        )

    # (key, name, min_pct, min_inclusive, max_pct, max_inclusive) — the same
    # continuous-partition shape as the Coggan power specs.
    specs: Final[
        tuple[tuple[str, str, float | None, bool, float | None, bool], ...]
    ] = (
        (_SWIM_ZONE_KEYS[0], _SWIM_ZONE_NAMES[0], None, False, boundaries[0], False),
        (
            _SWIM_ZONE_KEYS[1],
            _SWIM_ZONE_NAMES[1],
            boundaries[0],
            True,
            boundaries[1],
            False,
        ),
        (
            _SWIM_ZONE_KEYS[2],
            _SWIM_ZONE_NAMES[2],
            boundaries[1],
            True,
            boundaries[2],
            False,
        ),
        (
            _SWIM_ZONE_KEYS[3],
            _SWIM_ZONE_NAMES[3],
            boundaries[2],
            True,
            boundaries[3],
            False,
        ),
        (_SWIM_ZONE_KEYS[4], _SWIM_ZONE_NAMES[4], boundaries[3], True, None, False),
    )
    return tuple(
        SwimZone(
            key=key,
            name=name,
            min_pct_css=min_pct,
            min_pct_inclusive=min_inclusive,
            max_pct_css=max_pct,
            max_pct_inclusive=max_inclusive,
            # Pace axis mirrors the speed axis: the slower pace edge
            # (max_pace) comes from the speed minimum, the faster pace edge
            # (min_pace) from the speed maximum, inclusivity inverted.
            min_pace_sec_per_100m=(
                None if max_pct is None else _swim_pace_at_pct(max_pct, css_mps)
            ),
            min_pace_inclusive=max_inclusive,
            max_pace_sec_per_100m=(
                None if min_pct is None else _swim_pace_at_pct(min_pct, css_mps)
            ),
            max_pace_inclusive=min_inclusive,
        )
        for key, name, min_pct, min_inclusive, max_pct, max_inclusive in specs
    )


def swim_zone_for(
    pace_sec_per_100m: float,
    css_mps: float,
    *,
    boundary_pcts_css: tuple[float, float, float, float] = (
        DEFAULT_SWIM_ZONE_BOUNDARY_PCTS
    ),
) -> SwimZone:
    """Classify a pace per 100 m into its swim zone for ``css_mps`` (ZON-7).

    Consistent with :func:`swim_zones`: the pace is matched against the same
    table via :meth:`SwimZone.contains_pace` (the pace axis, exact for the
    table's own boundary paces). DIRECTION NOTE: a FASTER pace (fewer seconds
    per 100 m) is a HIGHER intensity — e.g. at the anchor-(a) CSS, 92.0 s
    /100 m is Threshold (Z4) while 105.0 s /100 m is Aerobic (Z2), and the
    CSS pace itself is exactly the Threshold boundary.

    ``pace_sec_per_100m`` and ``css_mps`` must be positive (``ValueError``);
    the boundary table is validated like :func:`swim_zones`.
    """
    if css_mps <= 0.0:
        raise ValueError(f"css_mps must be positive, got {css_mps!r}")
    if pace_sec_per_100m <= 0.0:
        raise ValueError(
            f"pace_sec_per_100m must be positive, got {pace_sec_per_100m!r}"
        )
    for zone in swim_zones(css_mps, boundary_pcts_css=boundary_pcts_css):
        if zone.contains_pace(pace_sec_per_100m):
            return zone
    raise ValueError(  # pragma: no cover - the table covers (0, inf) exactly
        f"no swim zone covers {pace_sec_per_100m!r} s /100 m at CSS "
        f"{css_mps!r} m/s; this should be unreachable"
    )


# ---------------------------------------------------------------------------
# Friel heart-rate zones per sport (ZON-8, PROJECT_BRIEF sections 7.3/12.4)
# ---------------------------------------------------------------------------

HrSportKey = Literal["run", "bike"]
"""Stable machine-readable keys of the two sports with Friel HR tables (§7.3)."""

HR_SPORT_KEYS: Final[tuple[HrSportKey, ...]] = ("run", "bike")
"""All heart-rate-zone sport keys (run and bike have separate Friel tables)."""

_HR_ZONE_NAMES: Final[tuple[str, str, str, str, str, str, str]] = (
    "Recovery",
    "Aerobic",
    "Tempo",
    "Sub-threshold",
    "VO2 max",
    "Anaerobic",
    "Maximum",
)
"""HR zone name labels, shared by both sports (OWNER CHOICE — the brief
publishes only the zone KEYS and percentages, not names)."""


_BOUNDARY_REL_TOL: Final = 1e-12
_BOUNDARY_ABS_TOL: Final = 1e-9


def _same_boundary(a: float, b: float) -> bool:
    """Whether two heart-rate bounds are the same boundary value.

    The zone tables round their absolute bpm bounds to two decimals, so a
    caller who computes a boundary himself (``0.95 * 169`` = 160.549999…)
    ends up one ULP away from the table's rounded literal (``160.55``).
    Comparing exactly made the classifier contradict its own table at
    95% and 106% of LTHR; the tolerances below treat those as equal while
    staying far tighter than any real heart-rate difference (1e-9 bpm).
    """
    return math.isclose(a, b, rel_tol=_BOUNDARY_REL_TOL, abs_tol=_BOUNDARY_ABS_TOL)


@dataclass(frozen=True, slots=True)
class HrZone:
    """One Friel heart-rate zone, printed percentages plus absolute bpm bounds.

    ``min_pct_lthr`` / ``max_pct_lthr`` are the EFFECTIVE percentage bounds
    of the zone under the documented continuous-partition gap rule (same rule
    as the Coggan power zones: see the module docstring); ``*_inclusive``
    state whether each bound belongs to the zone (Z1's top edge and Z5c's
    bottom edge are exclusive, exactly as printed: Z1 < first boundary,
    Z5c > last boundary). ``min_bpm`` / ``max_bpm`` are the same bounds in
    absolute bpm for the LTHR the table was built from (pct x LTHR / 100,
    unrounded). ``None`` means unbounded on that side. Frozen and slotted,
    consistent with :class:`PowerZone` and the other engine result
    conventions.
    """

    key: str
    name: str
    min_pct_lthr: float | None
    min_pct_inclusive: bool
    max_pct_lthr: float | None
    max_pct_inclusive: bool
    min_bpm: float | None
    max_bpm: float | None

    def contains_pct(self, pct: float) -> bool:
        """Whether a %LTHR value falls inside this zone under the gap rule."""
        below_min = self.min_pct_lthr is not None and (
            pct < self.min_pct_lthr
            or (not self.min_pct_inclusive and pct == self.min_pct_lthr)
        )
        if below_min:
            return False
        above_max = self.max_pct_lthr is not None and (
            pct > self.max_pct_lthr
            or (not self.max_pct_inclusive and pct == self.max_pct_lthr)
        )
        return not above_max

    def contains_bpm(self, bpm: float) -> bool:
        """Whether an absolute bpm value falls inside this zone (bpm axis).

        The comparison :func:`hr_zone_for` uses: matching against the table's
        own absolute bounds is exact for the table's boundary bpm values (no
        percentage round-trip), so a hand-exact printed boundary such as
        143.65 bpm at LTHR 169 classifies deterministically. Boundary values
        are compared tolerantly (:func:`_same_boundary`), so a caller who
        computes the boundary as ``0.95 * lthr`` instead of reading the
        rounded literal lands on the same zone.
        """
        at_min = self.min_bpm is not None and _same_boundary(bpm, self.min_bpm)
        if at_min:
            return self.min_pct_inclusive
        if self.min_bpm is not None and bpm < self.min_bpm:
            return False

        at_max = self.max_bpm is not None and _same_boundary(bpm, self.max_bpm)
        if at_max:
            return self.max_pct_inclusive
        above_max = self.max_bpm is not None and bpm > self.max_bpm
        return not above_max


# The two published Friel tables, exactly as printed in section 7.3 (% LTHR),
# as SEPARATE per-sport tables (they differ for Z1/Z2/Z3: run 85/89/94 vs
# bike 81/89/93). Effective bounds under the documented continuous-partition
# gap rule: each zone spans from its printed lower boundary value (inclusive)
# to the next zone's boundary value (exclusive); Z1 stays strictly below its
# first printed boundary and Z5c strictly above its last printed boundary
# (the printed 106 stays in Z5b because Z5c is strictly > 106).
# (key, name, min_pct, min_inclusive, max_pct, max_inclusive)
# LITERATURE: Friel, "The Triathlete's Training Bible" (zone names OWNER CHOICE).
_RunFrielSpec = tuple[str, str, float | None, bool, float | None, bool]
_RUN_FRIEL_ZONE_SPECS: Final[tuple[_RunFrielSpec, ...]] = (
    ("Z1", _HR_ZONE_NAMES[0], None, False, 85.0, False),
    ("Z2", _HR_ZONE_NAMES[1], 85.0, True, 90.0, False),
    ("Z3", _HR_ZONE_NAMES[2], 90.0, True, 95.0, False),
    ("Z4", _HR_ZONE_NAMES[3], 95.0, True, 100.0, False),
    ("Z5a", _HR_ZONE_NAMES[4], 100.0, True, 103.0, False),
    ("Z5b", _HR_ZONE_NAMES[5], 103.0, True, 106.0, True),
    ("Z5c", _HR_ZONE_NAMES[6], 106.0, False, None, False),
)
_BIKE_FRIEL_ZONE_SPECS: Final[tuple[_RunFrielSpec, ...]] = (
    ("Z1", _HR_ZONE_NAMES[0], None, False, 81.0, False),
    ("Z2", _HR_ZONE_NAMES[1], 81.0, True, 90.0, False),
    ("Z3", _HR_ZONE_NAMES[2], 90.0, True, 94.0, False),
    ("Z4", _HR_ZONE_NAMES[3], 94.0, True, 100.0, False),
    ("Z5a", _HR_ZONE_NAMES[4], 100.0, True, 103.0, False),
    ("Z5b", _HR_ZONE_NAMES[5], 103.0, True, 106.0, True),
    ("Z5c", _HR_ZONE_NAMES[6], 106.0, False, None, False),
)


def _hr_zones_for_specs(
    specs: tuple[_RunFrielSpec, ...], lthr_bpm: float
) -> tuple[HrZone, ...]:
    """Build absolute-bpm HrZone results from one sport's Friel spec table."""
    return tuple(
        HrZone(
            key=key,
            name=name,
            min_pct_lthr=min_pct,
            min_pct_inclusive=min_inclusive,
            max_pct_lthr=max_pct,
            max_pct_inclusive=max_inclusive,
            min_bpm=None if min_pct is None else min_pct * lthr_bpm / 100.0,
            max_bpm=None if max_pct is None else max_pct * lthr_bpm / 100.0,
        )
        for key, name, min_pct, min_inclusive, max_pct, max_inclusive in specs
    )


def hr_zones(sport: HrSportKey, lthr_bpm: float) -> tuple[HrZone, ...]:
    """The Friel heart-rate zone table for ``sport`` with absolute bpm bounds.

    The published percentages are reproduced EXACTLY (section 7.3, section
    12.4 acceptance) as SEPARATE per-sport tables in % of LTHR:

        Run : Z1 < 85, Z2 85-89, Z3 90-94, Z4 95-99,
              Z5a 100-102, Z5b 103-106, Z5c > 106
        Bike: Z1 < 81, Z2 81-89, Z3 90-93, Z4 94-99,
              Z5a 100-102, Z5b 103-106, Z5c > 106

    The tables differ for Z1/Z2/Z3 (run 85/89/94 vs bike 81/89/93) — see the
    test asserting that difference so a copy-paste mistake cannot pass.
    Absolute bpm bounds are pct x LTHR / 100, reported unrounded, from the
    athlete's per-sport LTHR (the owner's run/bike LTHR of record is
    169 bpm: 85% -> 143.65, 89% -> 150.41, 90% -> 152.10, 94% -> 158.86,
    95% -> 160.55, 99% -> 167.31, 100% -> 169.00, 102% -> 172.38,
    103% -> 174.07, 106% -> 179.14, plus the bike-only 81% -> 136.89 and
    93% -> 157.17).

    Gap rule for fractional percentages (the SAME documented
    continuous-partition rule as the Coggan power zones): the printed bounds
    leave gaps for values strictly between whole numbers (e.g. 89.5%). The
    implemented rule partitions the percentage space at the printed boundary
    values themselves: each zone spans from its printed lower boundary value
    (inclusive) to the next zone's boundary value (exclusive), while Z1 stays
    strictly below its first printed boundary and Z5c strictly above its last,
    exactly as printed — so Z2 = [85, 90) for run, Z5b = [103, 106] (the
    printed 106 stays in Z5b because Z5c is strictly > 106), and 89.5% is Z2.

    ``lthr_bpm`` must be positive (``ValueError``); an unknown ``sport``
    raises ``ValueError`` (defensive — the type limits it statically).

    Reference: Friel, "The Triathlete's Training Bible" (section 7.3,
    % LTHR tables per sport).
    """
    if sport not in HR_SPORT_KEYS:
        raise ValueError(
            f"unknown heart-rate sport {sport!r}; expected one of {HR_SPORT_KEYS}"
        )
    if lthr_bpm <= 0.0:
        raise ValueError(f"lthr_bpm must be positive, got {lthr_bpm!r}")
    specs = _RUN_FRIEL_ZONE_SPECS if sport == "run" else _BIKE_FRIEL_ZONE_SPECS
    return _hr_zones_for_specs(specs, lthr_bpm)


def hr_zone_for(sport: HrSportKey, bpm: float, lthr_bpm: float) -> HrZone:
    """Classify an absolute heart rate into its Friel zone for ``sport``.

    Consistent with :func:`hr_zones`: the bpm is matched against the same
    table via :meth:`HrZone.contains_bpm` (the absolute-bpm axis, exact for
    the table's boundary bpm values — e.g. at LTHR 169, 143.65 bpm is Z2,
    169.0 bpm is Z5a and 179.14 bpm is still Z5b because Z5c is strictly
    > 106% of LTHR).

    ``lthr_bpm`` must be positive and ``bpm`` non-negative (``ValueError``);
    an unknown ``sport`` raises ``ValueError`` (defensive — the type limits it
    statically).
    """
    if bpm < 0.0:
        raise ValueError(f"bpm must not be negative, got {bpm!r}")
    for zone in hr_zones(sport, lthr_bpm):
        if zone.contains_bpm(bpm):
            return zone
    raise ValueError(  # pragma: no cover - the table covers [0, inf) exactly
        f"no heart-rate zone covers {bpm!r} bpm at LTHR {lthr_bpm!r} for "
        f"sport {sport!r}; this should be unreachable"
    )


# ---------------------------------------------------------------------------
# Threshold change detection: propose, never apply (ZON-9, §7.3)
# ---------------------------------------------------------------------------

ThresholdMetricKey = Literal["ftp_watts", "cp_watts", "cs_mps", "css_mps"]
"""Stable machine-readable keys of the threshold metrics the model produces
(section 7.3): FTP, CP, run CS and swim CSS. All four are natively
"higher exceeds" quantities (watts, m/s), so a new effort "exceeds" the
model exactly when its native-unit value is greater. EXTENSIBILITY: add one
key here and one :data:`THRESHOLD_METRIC_UNITS` entry — the comparison,
margin semantics and outcome types below are metric-agnostic."""

THRESHOLD_METRIC_KEYS: Final[tuple[ThresholdMetricKey, ...]] = (
    "ftp_watts",
    "cp_watts",
    "cs_mps",
    "css_mps",
)
"""All threshold metric keys (the stable, validated metric namespace)."""

THRESHOLD_METRIC_UNITS: Final[Mapping[ThresholdMetricKey, str]] = {
    "ftp_watts": "W",
    "cp_watts": "W",
    "cs_mps": "m/s",
    "css_mps": "m/s",
}
"""Display unit per threshold metric (used in the evidence/detail text)."""

DEFAULT_THRESHOLD_CHANGE_MARGIN: Final[float] = 0.05
"""Default threshold-change margin: +5% RELATIVE to the current value.

OWNER CHOICE (pending owner confirmation), mirrored in Settings as
``ENGINE_THRESHOLD_CHANGE_MARGIN`` (ZON-11). A proposal requires the new
effort to exceed the current model value by STRICTLY more than this
fraction; the exact boundary is compared tolerantly (:func:`_same_boundary`)
so float noise cannot flip the outcome."""

NoProposalReason = Literal["no_improvement", "margin_not_exceeded"]
"""Machine-readable reasons of an explicit no-proposal outcome.

- ``"no_improvement"``: the new effort does not exceed the current value at
  all (``new <= current``).
- ``"margin_not_exceeded"``: the new effort is higher, but not by MORE than
  the margin (at or below the margin, boundary inclusive)."""

THRESHOLD_NO_PROPOSAL_REASONS: Final[tuple[NoProposalReason, ...]] = (
    "no_improvement",
    "margin_not_exceeded",
)
"""All machine-readable no-proposal reasons (stable keys for callers)."""

ThresholdProposalStatus = Literal["proposal_not_applied"]
"""The only status a proposal can carry: NOT applied (section 7.3).

A proposal is a SUGGESTION for the athlete to confirm (ZON-10 / Feature 6);
this module has no ability — and no entry point — to apply one."""


@dataclass(frozen=True, slots=True)
class ThresholdChangeProposal:
    """A PROPOSAL to raise a threshold, never an applied update (ZON-9).

    §7.3: "when a new effort exceeds the model by a configurable margin,
    propose (not apply) an update ... proposal object carries evidence and
    prior value". Accordingly this frozen result carries the proposed
    value, the prior (current) value, the delta and relative delta, the
    margin used, the evidence (the caller-supplied effort description plus
    the computed arithmetic) and the explicit ``status``
    ``"proposal_not_applied"`` — the type system's marker that nothing has
    been applied. There is no apply/commit capability on this type or
    anywhere in the engine; the athlete-confirmed application lives in the
    caller layer (ZON-10 / Feature 6).

    Frozen and slotted, consistent with the other engine result
    conventions.
    """

    metric: ThresholdMetricKey
    unit: str
    prior_value: float
    proposed_value: float
    delta: float
    relative_delta: float
    margin: float
    evidence: str
    status: ThresholdProposalStatus = "proposal_not_applied"


@dataclass(frozen=True, slots=True)
class NoThresholdChange:
    """Explicit "no proposal" outcome with a machine-readable reason (ZON-9).

    Returned (instead of ``None`` or an exception) whenever the new effort
    does NOT warrant a threshold proposal: no increase over the current
    value (``reason "no_improvement"``) or an increase at or below the
    margin (``reason "margin_not_exceeded"``; the exact boundary counts as
    not exceeded). Carries the same metric/prior/proposed/margin context as
    a proposal so callers can log or display the outcome uniformly. Frozen
    and slotted.
    """

    metric: ThresholdMetricKey
    prior_value: float
    proposed_value: float
    relative_delta: float
    margin: float
    reason: NoProposalReason
    detail: str


def propose_threshold_change(
    *,
    metric: ThresholdMetricKey,
    current_value: float,
    new_value: float,
    margin: float = DEFAULT_THRESHOLD_CHANGE_MARGIN,
    evidence: str = "",
) -> ThresholdChangeProposal | NoThresholdChange:
    """Compare a new effort against the current model value: propose or not.

    ZON-9 (section 7.3): returns a :class:`ThresholdChangeProposal` when
    ``new_value`` exceeds ``current_value`` by STRICTLY more than the
    relative ``margin`` (``(new - current) / current > margin``), and an
    explicit :class:`NoThresholdChange` otherwise (machine-readable reason,
    never ``None``, never a raise). The function is pure and INCAPABLE of
    applying anything — it computes a typed suggestion, nothing more (see
    the module docstring, "Threshold change detection").

    ``metric`` is one of :data:`THRESHOLD_METRIC_KEYS` (``ftp_watts``,
    ``cp_watts``, ``cs_mps``, ``css_mps``; unknown keys raise
    ``ValueError``). ``current_value`` and ``new_value`` must be positive
    and ``margin`` non-negative (``ValueError``). ``evidence`` is the
    caller's description of the effort that produced ``new_value`` (e.g.
    "best 20-min effort 275 W on 2026-03-01"); it is echoed into the
    proposal's ``evidence`` together with the computed arithmetic, so the
    §7.3 proposal object carries its justification.

    The margin is RELATIVE to the current value (default
    :data:`DEFAULT_THRESHOLD_CHANGE_MARGIN` = 5%, an OWNER CHOICE mirrored
    in Settings as ``ENGINE_THRESHOLD_CHANGE_MARGIN``). The exact boundary
    is compared tolerantly (:func:`_same_boundary`): a relative delta equal
    to the margin — even to float noise — is "margin_not_exceeded"; only a
    delta beyond the tolerance proposes.
    """
    if metric not in THRESHOLD_METRIC_KEYS:
        raise ValueError(
            f"unknown threshold metric {metric!r}; expected one of "
            f"{THRESHOLD_METRIC_KEYS}"
        )
    if current_value <= 0.0:
        raise ValueError(
            f"current_value must be positive, got {current_value!r} "
            f"({THRESHOLD_METRIC_UNITS[metric]} {metric!r})"
        )
    if new_value <= 0.0:
        raise ValueError(
            f"new_value must be positive, got {new_value!r} "
            f"({THRESHOLD_METRIC_UNITS[metric]} {metric!r})"
        )
    if margin < 0.0:
        raise ValueError(f"margin must not be negative, got {margin!r}")

    unit = THRESHOLD_METRIC_UNITS[metric]
    delta = new_value - current_value
    relative_delta = delta / current_value

    if delta <= 0.0:
        return NoThresholdChange(
            metric=metric,
            prior_value=current_value,
            proposed_value=new_value,
            relative_delta=relative_delta,
            margin=margin,
            reason="no_improvement",
            detail=(
                f"no threshold proposal for {metric!r}: the new effort "
                f"{new_value!r} {unit} does not exceed the current model "
                f"value {current_value!r} {unit} (relative delta "
                f"{relative_delta:+.1%})"
            ),
        )
    if relative_delta <= margin or _same_boundary(relative_delta, margin):
        return NoThresholdChange(
            metric=metric,
            prior_value=current_value,
            proposed_value=new_value,
            relative_delta=relative_delta,
            margin=margin,
            reason="margin_not_exceeded",
            detail=(
                f"no threshold proposal for {metric!r}: the new effort "
                f"{new_value!r} {unit} exceeds the current model value "
                f"{current_value!r} {unit} by {relative_delta:+.1%}, within "
                f"the {margin:.1%} margin (strictly more required)"
            ),
        )
    arithmetic = (
        f"{new_value!r} {unit} vs prior {current_value!r} {unit} "
        f"(+{relative_delta:.1%} > {margin:.1%} margin)"
    )
    evidence_text = f"{evidence}: {arithmetic}" if evidence else arithmetic
    return ThresholdChangeProposal(
        metric=metric,
        unit=unit,
        prior_value=current_value,
        proposed_value=new_value,
        delta=delta,
        relative_delta=relative_delta,
        margin=margin,
        evidence=evidence_text,
        status="proposal_not_applied",
    )
