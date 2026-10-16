"""Race prediction engine: run (Riegel + VDOT), bike (Martin et al. 1998
physics), swim (CSS), run-off-the-bike slowdown, transitions, and the Monte
Carlo composite triathlon total.

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core.settings`` (PROJECT_BRIEF sections 6 and 14). Athlete values and
course data come in as explicit parameters; side effects, persistence and
settings reads belong in callers.

CONTRACT (PROJECT_BRIEF sections 7.7 and 12.8): every prediction — segment
or total — returns a full :class:`PredictionEnvelope`
``{point, lower, upper, method, assumptions, model_version}`` with
``lower <= point <= upper`` (enforced in the dataclass). Never a bare
number: guessing race times is exactly the "generic chatbot invents
numbers" failure (section 2), so every envelope names its method, states
its assumptions and carries the model version.

Formulas and references
-----------------------
Run — Riegel (1981, "Athletic Records and Human Endurance", Exponential
Law):

    T2 = T1 * (D2 / D1)^1.06

with the exponent a documented constant (:data:`RIEGEL_EXPONENT`); the
published doubling factor is 2^1.06 = 2.08509 (5000 m in 20:00 -> 10000 m
in 41:42). The uncertainty band varies the exponent over its documented
range (exponent +/- :data:`RIEGEL_EXPONENT_UNCERTAINTY`), which models
athlete-caliber dependence of the exponent.

Run — VDOT equivalent: the VDOT of the reference effort is computed with
the Feature 4 Daniels & Gilbert functions of ``app.engine.zones``
(:func:`app.engine.zones.vdot_from_effort` — REUSED, never reimplemented),
and the target time is the duration whose VDOT equals it:

    vdot_from_effort(D2, T2) = vdot_from_effort(D1, T1)

Because ``percent_vo2max`` depends on the target duration itself, the
inversion is numerical (monotone bisection). Its band varies the target
VDOT by +/- :data:`VDOT_UNCERTAINTY` VDOT units. Both methods are returned
together with their spread — an honest display of model disagreement.

Bike — Martin et al. 1998 ("Effects of gradient and equipment on cycling
power", Journal of Biomechanics) power balance, exactly as printed in the
brief (section 7.7):

    P * eta = 0.5 * rho * CdA * v_air^2 * v
              + Crr * m * g * cos(theta) * v
              + m * g * sin(theta) * v

where ``v`` is ground speed, ``v_air = v + headwind`` the air-relative
speed, ``theta = atan(gradient)`` the road angle, ``eta`` the drivetrain
efficiency (default :data:`DEFAULT_DRIVETRAIN_EFFICIENCY` = 0.976,
LITERATURE). The solver finds the unique positive root per course segment
by bisection (the residual is strictly increasing in v for headwind >= 0
and eta > 0). Air density is derived from temperature and altitude with
the International Standard Atmosphere barometric formula and the ideal gas
law (LITERATURE: ISO 2533): rho = p(h) / (R_dry * T), with
p(h) = 101325 * (1 - 0.0065 h / 288.15)^5.25588 over the troposphere.

NOTE ON THE FLAT-COURSE CLOSED FORM: on a flat course in still air the
power equation is 0.5*rho*CdA*v^3 + Crr*m*g*v - eta*P = 0 — a depressed
CUBIC in v (the aero power term is a drag force times v). A pure
quadratic whose solution is ``v = sqrt(...)`` only arises when the drive
FORCE is given directly instead of the power; for the power form the
exact closed form is Cardano's formula for the depressed cubic, which
:func:`flat_course_speed_analytic` implements and which the numeric
solver must match (RP-3/§12.8 acceptance). The limiting cases are also
exact and hand-checkable: with Crr = 0, v = (2*eta*P/(rho*CdA))^(1/3);
with CdA = 0, v = eta*P/(Crr*m*g).

Swim — pace from Critical Swim Speed (CSS, Wakayoshi et al. 1992, Feature
4): the target speed is CSS times a distance-dependent fraction
(:data:`DEFAULT_SWIM_CSS_SPEED_FRACTIONS`, the CSS is a ~1500 m threshold
pace, so shorter swims run above it and long-course swims below it),
divided by an open-water adjustment factor
(:data:`DEFAULT_OPEN_WATER_FACTOR`) that slows the pace for sighting,
chop, drafting loss and the absence of push-off walls. The adjustment is a
documented ASSUMPTION (owner-reviewable), not a measured quantity.

Run off the bike — personal slowdown factor: the ratio of a brick/past-race
run time to the standalone Riegel prediction of the same run. With at
least :data:`MIN_RUN_OFF_BIKE_OBSERVATIONS` personal observations the
factor is their mean with a standard-error band; otherwise a configurable
prior (:data:`RUN_OFF_BIKE_PRIOR_MEAN`) is used with a deliberately
WIDENED band (:data:`RUN_OFF_BIKE_PRIOR_REL_SIGMA`). No unsourced constant
is hardcoded: every constant is module-level, named, and carries a source
citation or an explicit OWNER CHOICE marker (section 14).

Transitions — T1/T2 from past races (median of the observed times with a
standard-error band) or configurable per-distance defaults
(:data:`DEFAULT_TRANSITIONS_S`, OWNER CHOICE) with a widened band.

Triathlon total — the sum of the five parts (swim, bike, run off the bike,
T1, T2) with uncertainty propagated by Monte Carlo over documented input
distributions (section 7.7: distributions are documented assumptions).
The generator is ``numpy.random.default_rng(seed)`` with a FIXED default
seed (:data:`DEFAULT_MONTE_CARLO_SEED`), so bounds are stable across
calls; point estimates are the deterministic physics points, and
lower/upper are the 2.5th/97.5th percentiles of the samples (documented
OWNER CHOICE), always widened to contain the point.

Purity: all functions are pure and fully typed; validation errors raise
``ValueError`` rather than clamping or silently defaulting.

Configurable constants (RP tasks, §7/§14)
-----------------------------------------
Every constant below is a function parameter whose default is the
documented module constant; each carries a LITERATURE citation or an
explicit OWNER CHOICE marker. NOTHING here is mirrored into Settings —
the settings file is owned by another feature; wiring belongs to callers
(Feature 6).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from app.engine.zones import vdot_from_effort

__all__ = [
    "BIKE_POWER_REL_SIGMA",
    "CDA_REL_SIGMA",
    "CRR_REL_SIGMA",
    "DEFAULT_BIKE_POWER_FRACTION_FULL",
    "DEFAULT_BIKE_POWER_FRACTION_HALF",
    "DEFAULT_BIKE_POWER_FRACTION_OLYMPIC",
    "DEFAULT_BIKE_POWER_FRACTION_SPRINT",
    "DEFAULT_DRIVETRAIN_EFFICIENCY",
    "DEFAULT_GENERIC_TRANSITION_S",
    "DEFAULT_MONTE_CARLO_SAMPLES",
    "DEFAULT_MONTE_CARLO_SEED",
    "DEFAULT_OPEN_WATER_FACTOR",
    "DEFAULT_SWIM_CSS_SPEED_FRACTION_LONG",
    "DEFAULT_SWIM_CSS_SPEED_FRACTION_OLYMPIC",
    "DEFAULT_SWIM_CSS_SPEED_FRACTION_SPRINT",
    "DEFAULT_TRANSITIONS_S",
    "GRAVITY_M_S2",
    "MIN_RUN_OFF_BIKE_OBSERVATIONS",
    "MONTE_CARLO_LOWER_PERCENTILE",
    "MONTE_CARLO_UPPER_PERCENTILE",
    "PERSONAL_FACTOR_MIN_REL_SIGMA",
    "PERSONAL_TRANSITION_MIN_REL_SIGMA",
    "PREDICTION_MODEL_VERSION",
    "RIEGEL_EXPONENT",
    "RIEGEL_EXPONENT_UNCERTAINTY",
    "RUN_OFF_BIKE_PRIOR_MEAN",
    "RUN_OFF_BIKE_PRIOR_REL_SIGMA",
    "SWIM_CSS_REL_SIGMA",
    "TRANSITION_REL_SIGMA",
    "VDOT_UNCERTAINTY",
    "BikeCoursePrediction",
    "PredictionEnvelope",
    "RunPrediction",
    "TransitionsPrediction",
    "TriathlonPrediction",
    "air_density",
    "bike_course_prediction",
    "bike_speed_for_power",
    "default_bike_power_fraction",
    "default_swim_css_speed_fraction",
    "flat_course_speed_analytic",
    "predict_run",
    "required_bike_power",
    "riegel_time",
    "run_off_bike_factor",
    "swim_prediction",
    "transition_defaults_for",
    "transition_prediction",
    "triathlon_total_prediction",
    "vdot_equivalent_time",
]


# ---------------------------------------------------------------------------
# Prediction envelope contract (RP-9; PROJECT_BRIEF sections 7.7 / 12.8)
# ---------------------------------------------------------------------------

PREDICTION_MODEL_VERSION: Final[str] = "predict-1.0.0"
"""Model version carried by every prediction envelope.

OWNER CHOICE (engineering version marker): bumped whenever a formula,
constant default or distribution in this module changes meaningfully, so
stored predictions stay attributable to the model that produced them.
"""


@dataclass(frozen=True, slots=True)
class PredictionEnvelope:
    """Full structured prediction: never a bare number (sections 7.7/12.8).

    Exactly the six contract fields: ``point`` (the best estimate), ``lower``
    and ``upper`` (uncertainty bounds, ``lower <= point <= upper`` — enforced
    here, so a producer cannot return an inverted band), ``method`` (stable
    machine-readable name of the model), ``assumptions`` (human-readable
    assumptions and parameter provenance) and ``model_version``
    (:data:`PREDICTION_MODEL_VERSION`). Frozen and slotted, consistent with
    the other engine result conventions.

    Units are whatever the producing function documents (seconds for
    segment/total times, dimensionless for factors); the envelope itself is
    unit-agnostic.
    """

    point: float
    lower: float
    upper: float
    method: str
    assumptions: tuple[str, ...]
    model_version: str

    def __post_init__(self) -> None:
        if not self.lower <= self.point <= self.upper:
            raise ValueError(
                "prediction envelope invariant violated: need "
                f"lower <= point <= upper, got lower={self.lower!r}, "
                f"point={self.point!r}, upper={self.upper!r}"
            )
        if not self.assumptions:
            raise ValueError(
                "prediction envelope must state its assumptions; an empty "
                "assumptions tuple is a bare-number prediction in disguise"
            )


# ---------------------------------------------------------------------------
# Run prediction (RP-1/RP-2): Riegel + VDOT equivalent, both with spread
# ---------------------------------------------------------------------------

RIEGEL_EXPONENT: Final[float] = 1.06
"""Riegel distance-decay exponent for running.

LITERATURE: Riegel 1981, "Athletic Records and Human Endurance" (Exponential
Law), T2 = T1 * (D2/D1)^1.06; the same exponent is used by the equivalent
performance tables. Explicit function parameter of :func:`riegel_time`.
"""

RIEGEL_EXPONENT_UNCERTAINTY: Final[float] = 0.02
"""Half-width of the Riegel exponent uncertainty range (+/- 0.02).

OWNER CHOICE: Riegel's own fits show the exponent depends on athlete caliber
and distance (values ~1.04-1.08 are commonly reported for the same law), so
the band from varying the exponent over 1.04-1.08 is an honest, documented
model-spread assumption, not a measured distribution.
"""

VDOT_UNCERTAINTY: Final[float] = 1.0
"""Half-width of the VDOT uncertainty band, in VDOT units (+/- 1.0).

OWNER CHOICE: a single time-trial/race effort yields a VDOT with day-to-day
variability of roughly one Daniels VDOT unit (test-day fatigue, heat, course
terrain); the band from inverting VDOT +/- 1.0 documents that variability as
an explicit assumption rather than burying it.
"""


def riegel_time(
    reference_distance_m: float,
    reference_time_s: float,
    target_distance_m: float,
    *,
    exponent: float = RIEGEL_EXPONENT,
) -> float:
    """Riegel (1981) predicted time for ``target_distance_m``.

    Formula (LITERATURE: Riegel 1981, Exponential Law):

        T2 = T1 * (D2 / D1)^exponent        (exponent default 1.06)

    Published worked anchor: doubling the distance multiplies the time by
    2^1.06 = 2.08509 (5000 m in 20:00 -> 10000 m in 2502.11 s = 41:42);
    see ``tests/engine/test_predict.py``.

    ``reference_distance_m``, ``reference_time_s`` and ``target_distance_m``
    must be positive and ``exponent`` positive (``ValueError``).
    """
    if reference_distance_m <= 0.0:
        raise ValueError(
            f"reference distance must be positive, got {reference_distance_m!r} m"
        )
    if reference_time_s <= 0.0:
        raise ValueError(
            f"reference duration must be positive, got {reference_time_s!r} s"
        )
    if target_distance_m <= 0.0:
        raise ValueError(
            f"target distance must be positive, got {target_distance_m!r} m"
        )
    if exponent <= 0.0:
        raise ValueError(f"exponent must be positive, got {exponent!r}")
    return reference_time_s * float(
        (target_distance_m / reference_distance_m) ** exponent
    )


def _invert_vdot_time(target_distance_m: float, target_vdot: float) -> float:
    """Time on ``target_distance_m`` whose VDOT equals ``target_vdot``.

    Monotone bisection on t: for a fixed distance, a longer duration means a
    slower velocity, and the Daniels & Gilbert VDOT strictly decreases with
    the duration (VO2 falls like the velocity while %VO2max falls more
    slowly), so the residual has exactly one root. The bracket starts around
    the Riegel estimate (same physiology) and doubles outward until the root
    is enclosed; a non-positive target VDOT is invalid (``ValueError``).
    """
    if target_vdot <= 0.0:
        raise ValueError(
            f"target VDOT must be positive, got {target_vdot!r}"
        )
    guess = 500.0 * float((target_distance_m / 5000.0) ** RIEGEL_EXPONENT)
    lo = 0.2 * guess
    hi = 5.0 * guess
    while vdot_from_effort(target_distance_m, lo) < target_vdot:
        lo *= 0.5
    while vdot_from_effort(target_distance_m, hi) > target_vdot:
        hi *= 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if vdot_from_effort(target_distance_m, mid) > target_vdot:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9 * hi:
            break
    return 0.5 * (lo + hi)


def vdot_equivalent_time(
    reference_distance_m: float,
    reference_time_s: float,
    target_distance_m: float,
) -> float:
    """Time on ``target_distance_m`` with the reference effort's VDOT.

    The reference VDOT is :func:`app.engine.zones.vdot_from_effort` of
    ``(reference_distance_m, reference_time_s)`` (Feature 4 functions REUSED,
    never reimplemented); the target time is the unique duration ``T2`` with

        vdot_from_effort(D2, T2) = vdot_from_effort(D1, T1)

    found by monotone bisection (see :func:`_invert_vdot_time`; the
    ``percent_vo2max`` factor depends on the target duration, so the
    inversion is numerical, and its self-consistency is asserted by the
    tests). Inputs validated like :func:`riegel_time` (``ValueError``).
    """
    reference_vdot = vdot_from_effort(reference_distance_m, reference_time_s)
    return _invert_vdot_time(target_distance_m, reference_vdot)


@dataclass(frozen=True, slots=True)
class RunPrediction:
    """Run prediction by BOTH methods, with their spread (sections 7.7).

    ``riegel`` and ``vdot`` are full :class:`PredictionEnvelope` (seconds) —
    the Riegel band varies the exponent over its documented range, the VDOT
    band varies the target VDOT by :data:`VDOT_UNCERTAINTY`. ``spread_seconds``
    is |vdot.point - riegel.point|: an honest display of model disagreement.
    Frozen and slotted, consistent with the other engine result conventions.
    """

    riegel: PredictionEnvelope
    vdot: PredictionEnvelope
    spread_seconds: float


def predict_run(
    reference_distance_m: float,
    reference_time_s: float,
    target_distance_m: float,
) -> RunPrediction:
    """Run prediction via Riegel AND the VDOT equivalent, with spread.

    Returns a :class:`RunPrediction` whose two envelopes are:

    - ``riegel``: T2 = T1 * (D2/D1)^1.06 (:data:`RIEGEL_EXPONENT`);
      band from the exponent range
      ``1.06 +/- RIEGEL_EXPONENT_UNCERTAINTY`` — for a longer target the
      larger exponent is slower (upper), for a shorter target it is faster
      (lower), so the band always keeps ``lower <= point <= upper``.
    - ``vdot``: the :func:`vdot_equivalent_time` inversion; band from
      inverting the reference VDOT +/- :data:`VDOT_UNCERTAINTY` (a higher
      VDOT is a faster time, i.e. the lower bound).

    ``spread_seconds`` = |vdot.point - riegel.point|. Inputs validated like
    :func:`riegel_time` (``ValueError``).
    """
    riegel_point = riegel_time(
        reference_distance_m, reference_time_s, target_distance_m
    )
    exponent_low = RIEGEL_EXPONENT - RIEGEL_EXPONENT_UNCERTAINTY
    exponent_high = RIEGEL_EXPONENT + RIEGEL_EXPONENT_UNCERTAINTY
    time_low_exponent = riegel_time(
        reference_distance_m, reference_time_s, target_distance_m, exponent=exponent_low
    )
    time_high_exponent = riegel_time(
        reference_distance_m, reference_time_s, target_distance_m, exponent=exponent_high
    )
    riegel_lower = min(time_low_exponent, time_high_exponent)
    riegel_upper = max(time_low_exponent, time_high_exponent)

    reference_vdot = vdot_from_effort(reference_distance_m, reference_time_s)
    if reference_vdot <= VDOT_UNCERTAINTY:
        raise ValueError(
            "reference effort is too slow for a VDOT uncertainty band: VDOT "
            f"{reference_vdot!r} - VDOT_UNCERTAINTY {VDOT_UNCERTAINTY!r} would "
            "be non-positive"
        )
    vdot_point = _invert_vdot_time(target_distance_m, reference_vdot)
    vdot_lower = _invert_vdot_time(target_distance_m, reference_vdot + VDOT_UNCERTAINTY)
    vdot_upper = _invert_vdot_time(target_distance_m, reference_vdot - VDOT_UNCERTAINTY)

    return RunPrediction(
        riegel=PredictionEnvelope(
            point=riegel_point,
            lower=riegel_lower,
            upper=riegel_upper,
            method="riegel_1981",
            assumptions=(
                "T2 = T1 * (D2/D1)^1.06 (Riegel 1981, LITERATURE exponent)",
                "band: exponent 1.06 +/- 0.02 (OWNER CHOICE, athlete-caliber "
                "spread of the published exponent)",
            ),
            model_version=PREDICTION_MODEL_VERSION,
        ),
        vdot=PredictionEnvelope(
            point=vdot_point,
            lower=vdot_lower,
            upper=vdot_upper,
            method="vdot_inversion_daniels_gilbert",
            assumptions=(
                "VDOT(reference effort) held across the target distance "
                "(Daniels & Gilbert formulas reused from app.engine.zones, "
                "Feature 4)",
                "band: target VDOT +/- 1.0 unit (OWNER CHOICE, single-effort "
                "day-to-day variability)",
            ),
            model_version=PREDICTION_MODEL_VERSION,
        ),
        spread_seconds=abs(vdot_point - riegel_point),
    )


# ---------------------------------------------------------------------------
# Bike physics (RP-3/RP-4): Martin et al. 1998 power model
# ---------------------------------------------------------------------------

GRAVITY_M_S2: Final[float] = 9.80665
"""Standard acceleration of gravity, 9.80665 m/s^2.

LITERATURE: the ISO 80000-3 / BIPM standard gravity (the conventional value
used by the ISA and by Martin et al. 1998).
"""

DEFAULT_DRIVETRAIN_EFFICIENCY: Final[float] = 0.976
"""Drivetrain efficiency eta: fraction of pedal power reaching the wheel.

LITERATURE: Martin et al. 1998 (the ~2.4% loss measured for a clean,
lubricated derailleur drivetrain); the value fixed by the brief (section
7.7) as the default. Explicit parameter of every bike function.
"""

ISA_SEA_LEVEL_PRESSURE_PA: Final[float] = 101325.0
"""ISA sea-level standard pressure, 101325 Pa. LITERATURE: ISO 2533."""

ISA_SEA_LEVEL_TEMPERATURE_K: Final[float] = 288.15
"""ISA sea-level standard temperature, 288.15 K (15 °C). LITERATURE: ISO 2533."""

ISA_LAPSE_RATE_K_PER_M: Final[float] = 0.0065
"""ISA tropospheric temperature lapse rate, 0.0065 K/m. LITERATURE: ISO 2533."""

ISA_TROPOPAUSE_M: Final[float] = 11000.0
"""Upper validity limit of the ISA troposphere barometric formula, 11000 m.

LITERATURE: ISO 2533 (above the tropopause the lapse rate changes). Racing
altitudes are far below this; the limit is enforced so the formula is never
extrapolated past its derivation.
"""

DRY_AIR_GAS_CONSTANT: Final[float] = 287.058
"""Specific gas constant of dry air, 287.058 J/(kg*K). LITERATURE: ISO 2533."""


def air_density(temperature_c: float, altitude_m: float) -> float:
    """Air density (kg/m^3) from temperature and altitude (ISA, ideal gas).

    Barometric formula of the International Standard Atmosphere (LITERATURE:
    ISO 2533) for the pressure at altitude ``h`` in the troposphere:

        p(h) = 101325 * (1 - 0.0065 * h / 288.15)^(g * M / (R * 0.0065))

    (g = 9.80665 m/s^2, M = 0.0289644 kg/mol molar mass of air, R =
    8.314462618 J/(mol*K) universal gas constant; the exponent evaluates to
    5.25588). Combined with the ideal gas law with the dry-air specific gas
    constant R_dry = 287.058 J/(kg*K):

        rho = p(h) / (R_dry * T),  T = temperature_c + 273.15

    Published anchor: 15 °C at sea level -> rho = 1.225 kg/m^3 exactly (the
    standard ISA sea-level density; the tests pin it).

    ``temperature_c`` must be above absolute zero and ``altitude_m`` within
    the troposphere ``[0, 11000]`` m (``ValueError``) — the formula is never
    extrapolated past its derivation. Humidity is deliberately ignored: its
    effect on density is < 1% at racing conditions, and the assumption is
    stated in every bike prediction envelope.
    """
    if temperature_c <= -273.15:
        raise ValueError(
            "temperature must be above absolute zero (-273.15 °C), got "
            f"{temperature_c!r} °C"
        )
    if not 0.0 <= altitude_m <= ISA_TROPOPAUSE_M:
        raise ValueError(
            f"altitude must be within the troposphere [0, {ISA_TROPOPAUSE_M!r}] m "
            f"(ISA barometric formula validity), got {altitude_m!r} m"
        )
    exponent = GRAVITY_M_S2 * 0.0289644 / (8.314462618 * ISA_LAPSE_RATE_K_PER_M)
    pressure = ISA_SEA_LEVEL_PRESSURE_PA * float(
        (
            1.0 - ISA_LAPSE_RATE_K_PER_M * altitude_m / ISA_SEA_LEVEL_TEMPERATURE_K
        )
        ** exponent
    )
    temperature_k = temperature_c + 273.15
    return pressure / (DRY_AIR_GAS_CONSTANT * temperature_k)


def required_bike_power(
    speed_mps: float,
    *,
    gradient: float,
    cda: float,
    crr: float,
    mass_kg: float,
    air_density: float,
    drivetrain_efficiency: float = DEFAULT_DRIVETRAIN_EFFICIENCY,
    headwind_mps: float = 0.0,
) -> float:
    """Pedal power (W) needed to hold ``speed_mps`` (Martin et al. 1998).

    The power balance, exactly as printed in the brief (section 7.7;
    LITERATURE: Martin et al. 1998, "Effects of gradient and equipment on
    cycling power", Journal of Biomechanics):

        P * eta = 0.5 * rho * CdA * v_air^2 * v
                  + Crr * m * g * cos(theta) * v
                  + m * g * sin(theta) * v

    with ``v`` the ground speed, ``v_air = v + headwind`` the air-relative
    speed, ``theta = atan(gradient)`` the road angle (gradient = rise/run),
    ``eta`` the drivetrain efficiency. Solving for the pedal power:

        P = (0.5*rho*CdA*v_air^2 + Crr*m*g*cos(theta) + m*g*sin(theta)) * v / eta

    ``speed_mps`` must be positive, ``gradient > -1`` (rise/run; a gradient
    of -1 or below is a vertical drop, not a road), ``cda >= 0``, ``crr >= 0``,
    ``mass_kg`` positive, ``air_density`` positive and ``eta`` in (0, 1]
    (``ValueError``).
    """
    if speed_mps <= 0.0:
        raise ValueError(f"speed must be positive, got {speed_mps!r} m/s")
    if gradient <= -1.0:
        raise ValueError(
            f"gradient (rise/run) must be greater than -1, got {gradient!r}"
        )
    if cda < 0.0:
        raise ValueError(f"CdA must be non-negative, got {cda!r} m^2")
    if crr < 0.0:
        raise ValueError(f"Crr must be non-negative, got {crr!r}")
    if mass_kg <= 0.0:
        raise ValueError(f"total mass must be positive, got {mass_kg!r} kg")
    if air_density <= 0.0:
        raise ValueError(f"air density must be positive, got {air_density!r} kg/m^3")
    if not 0.0 < drivetrain_efficiency <= 1.0:
        raise ValueError(
            f"drivetrain efficiency must be within (0, 1], got {drivetrain_efficiency!r}"
        )
    if speed_mps + headwind_mps <= 0.0:
        raise ValueError(
            "air-relative speed v_air = v + headwind must be positive, got "
            f"v={speed_mps!r}, headwind={headwind_mps!r} m/s"
        )
    theta = math.atan(gradient)
    drag_force = 0.5 * air_density * cda * (speed_mps + headwind_mps) ** 2
    rolling_force = crr * mass_kg * GRAVITY_M_S2 * math.cos(theta)
    gravity_force = mass_kg * GRAVITY_M_S2 * math.sin(theta)
    return (drag_force + rolling_force + gravity_force) * speed_mps / drivetrain_efficiency


def _cbrt(x: float) -> float:
    """Real cube root (math has none for negative arguments)."""
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def flat_course_speed_analytic(
    target_power_w: float,
    *,
    cda: float,
    crr: float,
    mass_kg: float,
    air_density: float,
    drivetrain_efficiency: float = DEFAULT_DRIVETRAIN_EFFICIENCY,
) -> float:
    """Exact closed-form speed (m/s) on a flat course in still air.

    On a flat course (theta = 0) in still air (v_air = v) the power equation
    becomes a depressed CUBIC in v (the aero term is a drag force times v):

        A * v^3 + B * v - C = 0
        A = 0.5 * rho * CdA,  B = Crr * m * g,  C = eta * P

    NOTE (RP-3, §12.8): a pure quadratic with solution ``v = sqrt(...)`` only
    arises if the drive FORCE is specified directly instead of the pedal
    power; for the POWER form of Martin et al. 1998 the exact closed form is
    Cardano's formula for the depressed cubic (there is exactly one real root
    here because the residual is strictly increasing for v > 0):

        p = B / A,  q = -C / A,  D = (q/2)^2 + (p/3)^3
        v = cbrt(-q/2 + sqrt(D)) + cbrt(-q/2 - sqrt(D))

    Limiting cases (also exact, hand-checkable): Crr = 0 gives
    ``v = cbrt(2*eta*P / (rho*CdA))`` (the cube-root limit of the quadratic
    in v^2); CdA = 0 gives the linear ``v = eta*P / (Crr*m*g)``. The numeric
    solver (:func:`bike_speed_for_power`) must match this analytic value
    (RP-3 acceptance, asserted by the tests).

    ``target_power_w`` must be positive; ``cda``, ``crr`` non-negative with
    at least one strictly positive (with neither, any speed costs zero power
    and no steady state exists — ``ValueError``).
    """
    if target_power_w <= 0.0:
        raise ValueError(f"target power must be positive, got {target_power_w!r} W")
    if cda < 0.0 or crr < 0.0:
        raise ValueError("CdA and Crr must be non-negative")
    big_a = 0.5 * air_density * cda
    big_b = crr * mass_kg * GRAVITY_M_S2
    big_c = target_power_w * drivetrain_efficiency
    if big_a == 0.0 and big_b == 0.0:
        raise ValueError(
            "no resistance at all (CdA = Crr = 0): any speed costs zero power, "
            "so a steady-state speed does not exist"
        )
    if big_a == 0.0:
        return big_c / big_b
    p = big_b / big_a
    q = -big_c / big_a
    discriminant = (q / 2.0) ** 2 + (p / 3.0) ** 3
    return _cbrt(-q / 2.0 + math.sqrt(discriminant)) + _cbrt(
        -q / 2.0 - math.sqrt(discriminant)
    )


_SPEED_BRACKET_START_MPS: Final[float] = 1.0
"""Initial upper bracket (m/s) of the numeric speed solver; doubled outward.

ENGINEERING choice (not physics): pure numerics, no configurable meaning.
"""

_SPEED_BRACKET_MAX_DOUBLINGS: Final[int] = 60
"""Maximum bracket doublings of the numeric speed solver before giving up.

ENGINEERING choice: 2^60 m/s is beyond any physical speed, so exhaustion
means the inputs are inconsistent, and the solver raises instead of looping.
"""

_SPEED_TOLERANCE_MPS: Final[float] = 1e-9
"""Absolute bisection tolerance on speed (m/s).

ENGINEERING choice: far tighter than any measurement realism, cheap to reach
(~40 iterations from a 10 m/s bracket).
"""


def bike_speed_for_power(
    target_power_w: float,
    *,
    gradient: float,
    cda: float,
    crr: float,
    mass_kg: float,
    air_density: float,
    drivetrain_efficiency: float = DEFAULT_DRIVETRAIN_EFFICIENCY,
    headwind_mps: float = 0.0,
) -> float:
    """Steady-state speed (m/s) for ``target_power_w`` (Martin et al. 1998).

    Numerically inverts :func:`required_bike_power` by bisection: the pedal
    power is strictly increasing in the speed (for ``headwind_mps >= 0`` and
    ``eta > 0``), so the root is unique; the bracket starts at
    :data:`_SPEED_BRACKET_START_MPS` and doubles outward (at most
    :data:`_SPEED_BRACKET_MAX_DOUBLINGS` times) until the root is enclosed,
    then bisects to :data:`_SPEED_TOLERANCE_MPS`. On a flat course the
    result matches the analytic :func:`flat_course_speed_analytic` to
    solver tolerance (RP-3 acceptance, asserted by the tests).

    Validation identical to :func:`required_bike_power` plus a positive
    target power (``ValueError``).
    """
    if target_power_w <= 0.0:
        raise ValueError(f"target power must be positive, got {target_power_w!r} W")
    required_bike_power(
        1.0,  # validation probe: same parameter checks at a nominal speed
        gradient=gradient,
        cda=cda,
        crr=crr,
        mass_kg=mass_kg,
        air_density=air_density,
        drivetrain_efficiency=drivetrain_efficiency,
        headwind_mps=headwind_mps,
    )
    lo = 0.0
    hi = _SPEED_BRACKET_START_MPS
    for _ in range(_SPEED_BRACKET_MAX_DOUBLINGS):
        if (
            required_bike_power(
                hi,
                gradient=gradient,
                cda=cda,
                crr=crr,
                mass_kg=mass_kg,
                air_density=air_density,
                drivetrain_efficiency=drivetrain_efficiency,
                headwind_mps=headwind_mps,
            )
            > target_power_w
        ):
            break
        hi *= 2.0
    else:
        raise ValueError(
            "speed bracket exhausted: target power unreachable with the given "
            "inputs (is the target power enormous or the resistance zero?)"
        )
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if (
            required_bike_power(
                mid,
                gradient=gradient,
                cda=cda,
                crr=crr,
                mass_kg=mass_kg,
                air_density=air_density,
                drivetrain_efficiency=drivetrain_efficiency,
                headwind_mps=headwind_mps,
            )
            < target_power_w
        ):
            lo = mid
        else:
            hi = mid
        if hi - lo < _SPEED_TOLERANCE_MPS:
            break
    return 0.5 * (lo + hi)


DEFAULT_BIKE_POWER_FRACTION_SPRINT: Final[float] = 0.95
"""Target bike power as a fraction of FTP for sprint-distance bikes.

OWNER CHOICE (no single published number exists; consistent with standard
time-trial pacing guidance that a ~20 min bike leg is ridden just below
threshold): documented, configurable, never buried. See
:func:`default_bike_power_fraction` for the distance boundaries.
"""

DEFAULT_BIKE_POWER_FRACTION_OLYMPIC: Final[float] = 0.90
"""Target bike power fraction of FTP for olympic-distance bikes. OWNER CHOICE
(same basis as :data:`DEFAULT_BIKE_POWER_FRACTION_SPRINT`)."""

DEFAULT_BIKE_POWER_FRACTION_HALF: Final[float] = 0.85
"""Target bike power fraction of FTP for half-distance (70.3) bikes.
OWNER CHOICE (same basis as :data:`DEFAULT_BIKE_POWER_FRACTION_SPRINT`)."""

DEFAULT_BIKE_POWER_FRACTION_FULL: Final[float] = 0.80
"""Target bike power fraction of FTP for full-distance bikes. OWNER CHOICE
(same basis as :data:`DEFAULT_BIKE_POWER_FRACTION_SPRINT`)."""

_BIKE_FRACTION_BREAKS_M: Final[tuple[float, float, float]] = (
    30000.0,
    60000.0,
    120000.0,
)
"""Distance boundaries (m) of the power-fraction ladder: <= 30 km sprint
fraction, <= 60 km olympic fraction, <= 120 km half fraction, else full.

OWNER CHOICE: the sprint/olympic/half/full standard bike distances.
"""


def default_bike_power_fraction(distance_m: float) -> float:
    """Documented default target bike power as a fraction of FTP.

    Distance-dependent ladder (OWNER CHOICE, see the fraction constants):

        <= 30000 m -> 0.95,  <= 60000 m -> 0.90,
        <= 120000 m -> 0.85,  else -> 0.80

    ``distance_m`` must be positive (``ValueError``).
    """
    if distance_m <= 0.0:
        raise ValueError(f"distance must be positive, got {distance_m!r} m")
    if distance_m <= _BIKE_FRACTION_BREAKS_M[0]:
        return DEFAULT_BIKE_POWER_FRACTION_SPRINT
    if distance_m <= _BIKE_FRACTION_BREAKS_M[1]:
        return DEFAULT_BIKE_POWER_FRACTION_OLYMPIC
    if distance_m <= _BIKE_FRACTION_BREAKS_M[2]:
        return DEFAULT_BIKE_POWER_FRACTION_HALF
    return DEFAULT_BIKE_POWER_FRACTION_FULL


BIKE_POWER_REL_SIGMA: Final[float] = 0.03
"""Relative sigma of the sustainable target bike power (fraction of FTP).

OWNER CHOICE: day-to-day variability of a long steady effort (fuelling,
heat, pacing error) is taken as ~3%; drives the bike uncertainty band.
"""

_BIKE_BAND_SIGMA_MULTIPLE: Final[float] = 2.0
"""Band half-width in sigmas for the analytic bike band: +/- 2 sigma ~ the
central 95% under the documented normal assumption. OWNER CHOICE."""


@dataclass(frozen=True, slots=True)
class BikeCoursePrediction:
    """Bike prediction over a gradient profile: one envelope per segment plus
    the total (seconds). Frozen and slotted, consistent with the other engine
    result conventions."""

    segments: tuple[PredictionEnvelope, ...]
    total: PredictionEnvelope


def bike_course_prediction(
    segments: Sequence[tuple[float, float]],
    *,
    cda: float,
    crr: float,
    mass_kg: float,
    temperature_c: float,
    altitude_m: float,
    ftp_watts: float | None = None,
    target_power_w: float | None = None,
    power_fraction: float | None = None,
    drivetrain_efficiency: float = DEFAULT_DRIVETRAIN_EFFICIENCY,
) -> BikeCoursePrediction:
    """Per-segment bike times over a gradient profile, with bounds.

    ``segments`` is a sequence of ``(distance_m, gradient)`` pairs (gradient
    = rise/run). The target power is EITHER an explicit ``target_power_w``
    OR an ``ftp_watts`` scaled by a fraction of FTP — the fraction is
    ``power_fraction`` when given, otherwise the documented distance ladder
    (:func:`default_bike_power_fraction`, OWNER CHOICE). Exactly one power
    source must be given (``ValueError``).

    For every segment the point time solves the Martin et al. 1998 balance
    (:func:`bike_speed_for_power`) with the air density from
    :func:`air_density` (temperature and altitude inputs). The band comes
    from varying the target power by +/-
    ``_BIKE_BAND_SIGMA_MULTIPLE * BIKE_POWER_REL_SIGMA`` (documented OWNER
    CHOICE: power sustainability is the dominant uncertainty; CdA and Crr
    are taken as known course/athlete inputs) — more power is a faster time
    (the lower bound). The total is the sum of the segment times.

    ``segments`` must be non-empty, each distance positive and each gradient
    > -1 (``ValueError``).
    """
    if not segments:
        raise ValueError("segments must not be empty: no course was given")
    if (target_power_w is None) == (ftp_watts is None):
        raise ValueError(
            "exactly one bike power source is required: give ftp_watts (scaled "
            "by a distance-dependent fraction of FTP) or an explicit "
            "target_power_w, not both and not neither"
        )
    total_distance_m = math.fsum(distance for distance, _ in segments)
    if target_power_w is not None:
        if target_power_w <= 0.0:
            raise ValueError(f"target power must be positive, got {target_power_w!r} W")
        nominal_power = target_power_w
    else:
        assert ftp_watts is not None
        if ftp_watts <= 0.0:
            raise ValueError(f"FTP must be positive, got {ftp_watts!r} W")
        fraction = (
            power_fraction
            if power_fraction is not None
            else default_bike_power_fraction(total_distance_m)
        )
        if fraction <= 0.0:
            raise ValueError(f"power fraction must be positive, got {fraction!r}")
        nominal_power = ftp_watts * fraction
    rho = air_density(temperature_c, altitude_m)

    def _segment_time(
        segment_distance: float, segment_gradient: float, power: float
    ) -> float:
        speed = bike_speed_for_power(
            power,
            gradient=segment_gradient,
            cda=cda,
            crr=crr,
            mass_kg=mass_kg,
            air_density=rho,
            drivetrain_efficiency=drivetrain_efficiency,
        )
        return segment_distance / speed

    power_low = nominal_power * (1.0 - _BIKE_BAND_SIGMA_MULTIPLE * BIKE_POWER_REL_SIGMA)
    power_high = nominal_power * (1.0 + _BIKE_BAND_SIGMA_MULTIPLE * BIKE_POWER_REL_SIGMA)
    if power_low <= 0.0:
        raise ValueError(
            "power band lower edge is non-positive: reduce the band sigmas or "
            "raise the target power"
        )

    segment_envelopes: list[PredictionEnvelope] = []
    for distance, gradient in segments:
        if distance <= 0.0:
            raise ValueError(f"segment distance must be positive, got {distance!r} m")
        point = _segment_time(distance, gradient, nominal_power)
        lower = _segment_time(distance, gradient, power_high)
        upper = _segment_time(distance, gradient, power_low)
        segment_envelopes.append(
            PredictionEnvelope(
                point=point,
                lower=lower,
                upper=upper,
                method="martin_1998_numeric_bisection",
                assumptions=(
                    "P*eta = 0.5*rho*CdA*v_air^2*v + Crr*m*g*cos(theta)*v + "
                    "m*g*sin(theta)*v solved numerically (Martin et al. 1998)",
                    f"rho = {rho:.4f} kg/m^3 from ISA (ISO 2533) at "
                    f"{temperature_c} °C, {altitude_m} m; humidity ignored (<1%)",
                    "steady state per segment, no accelerations, no drafting",
                    "band: target power +/- 2*0.03 relative (OWNER CHOICE); "
                    "CdA and Crr taken as known",
                ),
                model_version=PREDICTION_MODEL_VERSION,
            )
        )
    total_point = math.fsum(e.point for e in segment_envelopes)
    total_lower = math.fsum(e.lower for e in segment_envelopes)
    total_upper = math.fsum(e.upper for e in segment_envelopes)
    return BikeCoursePrediction(
        segments=tuple(segment_envelopes),
        total=PredictionEnvelope(
            point=total_point,
            lower=total_lower,
            upper=total_upper,
            method="martin_1998_numeric_bisection_course_sum",
            assumptions=(
                "sum of the per-segment steady-state solutions (no "
                "acceleration between segments)",
                "band: sum of the per-segment power bands (no cancellation "
                "assumed between segments)",
            ),
            model_version=PREDICTION_MODEL_VERSION,
        ),
    )


# ---------------------------------------------------------------------------
# Swim prediction (RP-5)
# ---------------------------------------------------------------------------

DEFAULT_SWIM_CSS_SPEED_FRACTION_SPRINT: Final[float] = 1.05
"""Swim speed as a fraction of CSS for sprint-distance swims (<= 1000 m).

OWNER CHOICE: CSS (Wakayoshi et al. 1992) is a ~1500 m threshold pace, so a
short race swim is swum slightly above it.
"""

DEFAULT_SWIM_CSS_SPEED_FRACTION_OLYMPIC: Final[float] = 1.00
"""Swim speed fraction of CSS for olympic-distance swims (~1500 m): CSS
itself. LITERATURE-grounded anchor (CSS IS the ~1500 m threshold intensity)."""

DEFAULT_SWIM_CSS_SPEED_FRACTION_LONG: Final[float] = 0.97
"""Swim speed fraction of CSS for long-course swims (> 2000 m). OWNER CHOICE:
half/full swim legs are paced slightly below the threshold intensity."""

_SWIM_FRACTION_BREAKS_M: Final[tuple[float, float]] = (1000.0, 2000.0)
"""Distance boundaries (m) of the swim CSS-fraction ladder (OWNER CHOICE,
bracketing the olympic 1500 m swim)."""

DEFAULT_OPEN_WATER_FACTOR: Final[float] = 1.03
"""Open-water slowdown factor applied to the CSS-derived speed (>= 1).

DOCUMENTED ASSUMPTION (OWNER CHOICE, configurable per call): open water costs
~3% speed versus a pool CSS of record through sighting, chop, swell and the
absence of push-off walls. No published universal number exists; the factor
is an explicit, reviewable assumption.
"""

SWIM_CSS_REL_SIGMA: Final[float] = 0.02
"""Relative sigma of the CSS of record for the swim band. OWNER CHOICE: a
CSS from two time trials carries ~2% day-to-day variability."""

_SWIM_BAND_SIGMA_MULTIPLE: Final[float] = 2.0
"""Band half-width in sigmas for the swim band (+/- 2 sigma ~ 95%).
OWNER CHOICE."""


def default_swim_css_speed_fraction(distance_m: float) -> float:
    """Documented default swim speed as a fraction of CSS for ``distance_m``.

    Distance ladder (OWNER CHOICE, see the fraction constants):

        <= 1000 m -> 1.05,  <= 2000 m -> 1.00,  else -> 0.97

    ``distance_m`` must be positive (``ValueError``).
    """
    if distance_m <= 0.0:
        raise ValueError(f"distance must be positive, got {distance_m!r} m")
    if distance_m <= _SWIM_FRACTION_BREAKS_M[0]:
        return DEFAULT_SWIM_CSS_SPEED_FRACTION_SPRINT
    if distance_m <= _SWIM_FRACTION_BREAKS_M[1]:
        return DEFAULT_SWIM_CSS_SPEED_FRACTION_OLYMPIC
    return DEFAULT_SWIM_CSS_SPEED_FRACTION_LONG


def swim_prediction(
    distance_m: float,
    css_mps: float,
    *,
    css_speed_fraction: float | None = None,
    open_water_factor: float = DEFAULT_OPEN_WATER_FACTOR,
    css_rel_sigma: float = SWIM_CSS_REL_SIGMA,
) -> PredictionEnvelope:
    """Swim time (s) for ``distance_m`` from the CSS of record, with bounds.

    Speed model (RP-5): the target speed is the CSS times a
    distance-dependent fraction (:func:`default_swim_css_speed_fraction`
    when not given explicitly), divided by the open-water adjustment factor
    (a DOCUMENTED ASSUMPTION, :data:`DEFAULT_OPEN_WATER_FACTOR` — sighting,
    chop and no push-off walls slow the pace; configurable,
    owner-reviewable):

        v = CSS * fraction(distance) / open_water_factor
        T = distance / v

    The band varies the CSS by +/- _SWIM_BAND_SIGMA_MULTIPLE * css_rel_sigma
    (a higher CSS is a faster time, i.e. the lower bound).

    ``distance_m`` and ``css_mps`` must be positive, the resolved fraction
    positive, ``open_water_factor >= 1`` and ``css_rel_sigma`` within
    ``(0, 1)`` (``ValueError``).
    """
    if distance_m <= 0.0:
        raise ValueError(f"distance must be positive, got {distance_m!r} m")
    if css_mps <= 0.0:
        raise ValueError(f"CSS must be positive, got {css_mps!r} m/s")
    if css_speed_fraction is None:
        css_speed_fraction = default_swim_css_speed_fraction(distance_m)
    if css_speed_fraction <= 0.0:
        raise ValueError(
            f"CSS speed fraction must be positive, got {css_speed_fraction!r}"
        )
    if open_water_factor < 1.0:
        raise ValueError(
            "open_water_factor must be >= 1 (it slows the pool CSS; open water "
            f"is never faster than the pool), got {open_water_factor!r}"
        )
    if not 0.0 < css_rel_sigma < 1.0:
        raise ValueError(
            f"css_rel_sigma must be within (0, 1), got {css_rel_sigma!r}"
        )
    speed = css_mps * css_speed_fraction / open_water_factor
    point = distance_m / speed
    css_multiple_low = 1.0 - _SWIM_BAND_SIGMA_MULTIPLE * css_rel_sigma
    css_multiple_high = 1.0 + _SWIM_BAND_SIGMA_MULTIPLE * css_rel_sigma
    lower = distance_m / (speed * css_multiple_high)
    upper = distance_m / (speed * css_multiple_low)
    return PredictionEnvelope(
        point=point,
        lower=lower,
        upper=upper,
        method="css_fraction_open_water",
        assumptions=(
            f"speed = CSS * {css_speed_fraction:g} / {open_water_factor:g} "
            "(distance-dependent CSS fraction, OWNER CHOICE; open-water "
            "slowdown is a documented ASSUMPTION: sighting, chop, no walls)",
            "band: CSS +/- 2*0.02 relative (OWNER CHOICE, two-time-trial "
            "day-to-day variability)",
        ),
        model_version=PREDICTION_MODEL_VERSION,
    )


# ---------------------------------------------------------------------------
# Run-off-the-bike slowdown (RP-6)
# ---------------------------------------------------------------------------

MIN_RUN_OFF_BIKE_OBSERVATIONS: Final[int] = 2
"""Minimum number of personal brick/past-race observations before the
PERSONAL factor replaces the prior. OWNER CHOICE: one observation cannot
separate the athlete from the population prior."""

RUN_OFF_BIKE_PRIOR_MEAN: Final[float] = 1.06
"""Prior mean of the run-off-the-bike slowdown factor (dimensionless).

OWNER CHOICE (NO unsourced constant is hardcoded as truth): triathlon
coaching practice places the brick-run penalty around 4-8% versus the same
run raced fresh; 1.06 sits mid-range and is explicitly configurable. The
factor multiplies the standalone run prediction.
"""

RUN_OFF_BIKE_PRIOR_REL_SIGMA: Final[float] = 0.05
"""Prior relative sigma of the run-off-the-bike factor — the WIDENED band
used while personal data is insufficient (RP-6). OWNER CHOICE: deliberately
wide because it is a population prior, not the athlete's own distribution.
"""

PERSONAL_FACTOR_MIN_REL_SIGMA: Final[float] = 0.01
"""Floor on the personal factor's relative sigma (the standard error of the
mean collapses onto the point for near-identical observations). OWNER CHOICE."""

_BAND_SIGMA_MULTIPLE: Final[float] = 2.0
"""Band half-width in sigmas for factor bands: +/- 2 sigma ~ the central 95%
under the documented normal assumption. OWNER CHOICE."""


def run_off_bike_factor(
    observations: Sequence[float] = (),
    *,
    min_observations: int = MIN_RUN_OFF_BIKE_OBSERVATIONS,
    prior_mean: float = RUN_OFF_BIKE_PRIOR_MEAN,
    prior_rel_sigma: float = RUN_OFF_BIKE_PRIOR_REL_SIGMA,
) -> PredictionEnvelope:
    """Run-off-the-bike slowdown factor (dimensionless envelope), RP-6.

    ``observations`` are personal slowdown ratios (brick/past-race run time
    divided by the standalone Riegel prediction of the same run). With at
    least ``min_observations`` of them the factor is their MEAN with a band
    from the standard error of the mean (floored at
    :data:`PERSONAL_FACTOR_MIN_REL_SIGMA`); otherwise the configurable prior
    (:data:`RUN_OFF_BIKE_PRIOR_MEAN`) is used with its deliberately WIDENED
    band (:data:`RUN_OFF_BIKE_PRIOR_REL_SIGMA`) — the insufficient-data path
    never narrows to the prior point.

    All constants are module-level, named, and carry OWNER CHOICE markers:
    no unsourced constant is hardcoded (RP-6 acceptance). Each observation
    must be positive and ``min_observations >= 1`` (``ValueError``).
    """
    if min_observations < 1:
        raise ValueError(
            f"min_observations must be at least 1, got {min_observations!r}"
        )
    if prior_mean <= 0.0:
        raise ValueError(f"prior mean must be positive, got {prior_mean!r}")
    if not 0.0 < prior_rel_sigma < 1.0:
        raise ValueError(
            f"prior relative sigma must be within (0, 1), got {prior_rel_sigma!r}"
        )
    observations = tuple(observations)
    if any(obs <= 0.0 for obs in observations):
        raise ValueError(
            f"every observation must be positive, got {observations!r}"
        )
    if len(observations) >= min_observations:
        n = float(len(observations))
        mean = math.fsum(observations) / n
        if n > 1.0:
            variance = math.fsum((obs - mean) ** 2 for obs in observations) / (
                n - 1.0
            )
            rel_sigma = max(
                math.sqrt(variance / n) / mean,
                PERSONAL_FACTOR_MIN_REL_SIGMA,
            )
        else:
            # A single observation has no sample variance: the floor sigma
            # stands (the point is the observation, the band is honest).
            rel_sigma = PERSONAL_FACTOR_MIN_REL_SIGMA
        return PredictionEnvelope(
            point=mean,
            lower=mean * (1.0 - _BAND_SIGMA_MULTIPLE * rel_sigma),
            upper=mean * (1.0 + _BAND_SIGMA_MULTIPLE * rel_sigma),
            method="personal_brick_data",
            assumptions=(
                f"factor = mean of {len(observations)} personal brick/race "
                "observations (run time / standalone Riegel prediction)",
                "band: +/- 2 standard errors of the mean, floored at "
                f"{PERSONAL_FACTOR_MIN_REL_SIGMA:g} relative (OWNER CHOICE)",
            ),
            model_version=PREDICTION_MODEL_VERSION,
        )
    return PredictionEnvelope(
        point=prior_mean,
        lower=prior_mean * (1.0 - _BAND_SIGMA_MULTIPLE * prior_rel_sigma),
        upper=prior_mean * (1.0 + _BAND_SIGMA_MULTIPLE * prior_rel_sigma),
        method="prior_widened",
        assumptions=(
            f"factor = configurable prior {prior_mean:g} (OWNER CHOICE, "
            "coaching practice range 4-8%)",
            "band WIDENED to +/- 2*0.05 relative while personal data is "
            f"insufficient (< {min_observations} observations, RP-6)",
        ),
        model_version=PREDICTION_MODEL_VERSION,
    )


# ---------------------------------------------------------------------------
# Transitions (RP-7)
# ---------------------------------------------------------------------------

TRANSITION_REL_SIGMA: Final[float] = 0.30
"""Relative sigma of the transition-time band when using the defaults — the
WIDENED band of a population prior (transition competence varies widely).
OWNER CHOICE."""

PERSONAL_TRANSITION_MIN_REL_SIGMA: Final[float] = 0.05
"""Floor on the personal transition band's relative sigma. OWNER CHOICE."""

DEFAULT_TRANSITIONS_S: Final[dict[str, tuple[float, float]]] = {
    "sprint": (180.0, 90.0),
    "olympic": (240.0, 120.0),
    "half": (300.0, 150.0),
    "full": (360.0, 180.0),
}
"""Configurable default T1/T2 in seconds per race distance key (T1 =
swim-to-bike including the wetsuit strip, T2 = bike-to-run). OWNER CHOICE —
starting points for an age-group athlete, explicitly configurable per call.
"""

DEFAULT_GENERIC_TRANSITION_S: Final[tuple[float, float]] = (300.0, 150.0)
"""Default T1/T2 when no race key is known. OWNER CHOICE (the half-distance
row of :data:`DEFAULT_TRANSITIONS_S`)."""

_TRANSITION_KEYS: Final[tuple[str, ...]] = (
    "sprint",
    "olympic",
    "half",
    "full",
)
"""Valid race distance keys for the transition defaults."""


@dataclass(frozen=True, slots=True)
class TransitionsPrediction:
    """T1 and T2 predictions, each a full envelope (seconds). Frozen and
    slotted, consistent with the other engine result conventions."""

    t1: PredictionEnvelope
    t2: PredictionEnvelope


def transition_defaults_for(race_key: str) -> tuple[float, float]:
    """Configurable (T1, T2) defaults in seconds for ``race_key``.

    ``race_key`` must be one of :data:`_TRANSITION_KEYS` (``ValueError``);
    the values are the documented OWNER CHOICE defaults
    (:data:`DEFAULT_TRANSITIONS_S`).
    """
    if race_key not in DEFAULT_TRANSITIONS_S:
        raise ValueError(
            f"unknown race key {race_key!r}; expected one of {_TRANSITION_KEYS}"
        )
    return DEFAULT_TRANSITIONS_S[race_key]


def _transition_envelope(
    past_transitions_s: Sequence[float],
    default_s: float,
    *,
    kind: str,
) -> PredictionEnvelope:
    """Shared T1/T2 envelope: observed median, or the default with a widened
    band (RP-7). Generalised once, never duplicated."""
    if default_s <= 0.0:
        raise ValueError(f"default {kind} must be positive, got {default_s!r}")
    observations = tuple(past_transitions_s)
    if any(obs <= 0.0 for obs in observations):
        raise ValueError(
            f"every past {kind} must be positive, got {observations!r}"
        )
    if observations:
        ordered = sorted(observations)
        n = len(ordered)
        mid = n // 2
        median = (
            ordered[mid] if n % 2 == 1 else 0.5 * (ordered[mid - 1] + ordered[mid])
        )
        mean = math.fsum(ordered) / n
        if n > 1:
            variance = math.fsum((obs - mean) ** 2 for obs in ordered) / (n - 1)
            rel_sigma = max(
                math.sqrt(variance / n) / mean,
                PERSONAL_TRANSITION_MIN_REL_SIGMA,
            )
        else:
            # A single observation has no sample variance: the floor sigma
            # stands (the point is the observation, the band is honest).
            rel_sigma = PERSONAL_TRANSITION_MIN_REL_SIGMA
        return PredictionEnvelope(
            point=median,
            lower=median * (1.0 - _BAND_SIGMA_MULTIPLE * rel_sigma),
            upper=median * (1.0 + _BAND_SIGMA_MULTIPLE * rel_sigma),
            method=f"observed_{kind}_median",
            assumptions=(
                f"{kind} = median of {n} past-race observations",
                "band: +/- 2 standard errors of the mean, floored at "
                f"{PERSONAL_TRANSITION_MIN_REL_SIGMA:g} relative (OWNER CHOICE)",
            ),
            model_version=PREDICTION_MODEL_VERSION,
        )
    return PredictionEnvelope(
        point=default_s,
        lower=default_s * (1.0 - _BAND_SIGMA_MULTIPLE * TRANSITION_REL_SIGMA),
        upper=default_s * (1.0 + _BAND_SIGMA_MULTIPLE * TRANSITION_REL_SIGMA),
        method=f"default_{kind}_prior",
        assumptions=(
            f"{kind} = configurable default {default_s:g} s (OWNER CHOICE)",
            "band WIDENED to +/- 2*0.30 relative (OWNER CHOICE population "
            "prior: transition competence varies widely)",
        ),
        model_version=PREDICTION_MODEL_VERSION,
    )


def transition_prediction(
    past_t1_s: Sequence[float] = (),
    past_t2_s: Sequence[float] = (),
    *,
    race_key: str | None = None,
    default_t1_s: float | None = None,
    default_t2_s: float | None = None,
) -> TransitionsPrediction:
    """T1/T2 predictions (s) from past races or configurable defaults, RP-7.

    Per transition: when past-race times exist, the MEDIAN with a standard-
    error band; otherwise the configurable default for ``race_key``
    (:data:`DEFAULT_TRANSITIONS_S`, OWNER CHOICE; falls back to
    :data:`DEFAULT_GENERIC_TRANSITION_S` when no key is given) with the
    widened :data:`TRANSITION_REL_SIGMA` band. Explicit ``default_t1_s`` /
    ``default_t2_s`` override the race-key lookup (``ValueError`` on
    non-positive values, non-positive observations or unknown keys).
    """
    if race_key is None:
        key_default_t1, key_default_t2 = DEFAULT_GENERIC_TRANSITION_S
    else:
        key_default_t1, key_default_t2 = transition_defaults_for(race_key)
    default_t1 = default_t1_s if default_t1_s is not None else key_default_t1
    default_t2 = default_t2_s if default_t2_s is not None else key_default_t2
    return TransitionsPrediction(
        t1=_transition_envelope(past_t1_s, default_t1, kind="t1"),
        t2=_transition_envelope(past_t2_s, default_t2, kind="t2"),
    )


# ---------------------------------------------------------------------------
# Composite triathlon total (RP-8): Monte Carlo uncertainty propagation
# ---------------------------------------------------------------------------

CDA_REL_SIGMA: Final[float] = 0.03
"""Relative sigma of the CdA of record (position drift between sessions).
OWNER CHOICE, documented Monte Carlo input distribution (section 7.7)."""

CRR_REL_SIGMA: Final[float] = 0.10
"""Relative sigma of the rolling resistance (surface/tyre variability).
OWNER CHOICE, documented Monte Carlo input distribution (section 7.7)."""

DEFAULT_MONTE_CARLO_SEED: Final[int] = 20261016
"""Fixed default Monte Carlo seed: bounds are STABLE across calls (RP-8).

OWNER CHOICE: an arbitrary but fixed integer (the date of the owner's
feature decision, ODD log L1); a caller may pass a different seed, but the
default must never change silently or stored bounds would drift.
"""

DEFAULT_MONTE_CARLO_SAMPLES: Final[int] = 4000
"""Default Monte Carlo sample count. OWNER CHOICE: percentile noise of the
2.5/97.5 bounds is well below 0.1% of the total at 4000 samples, and the
vectorized solver keeps the cost in milliseconds."""

MONTE_CARLO_LOWER_PERCENTILE: Final[float] = 2.5
"""Lower percentile of the Monte Carlo band (~95% central interval).
OWNER CHOICE."""

MONTE_CARLO_UPPER_PERCENTILE: Final[float] = 97.5
"""Upper percentile of the Monte Carlo band (~95% central interval).
OWNER CHOICE."""

_RUN_EXPONENT_SAMPLING_UNCERTAINTY: Final[float] = RIEGEL_EXPONENT_UNCERTAINTY
"""The run exponent is sampled uniformly over the same documented range the
Riegel envelope band uses (1.06 +/- 0.02). Documented MC distribution."""

_MC_VECTOR_SPEED_TOLERANCE_MPS: Final[float] = 1e-9
"""Bisection tolerance (m/s) of the vectorized Monte Carlo solver.
ENGINEERING choice, mirrors _SPEED_TOLERANCE_MPS."""

_MC_VECTOR_MAX_DOUBLINGS: Final[int] = 60
"""Maximum bracket doublings of the vectorized Monte Carlo solver.
ENGINEERING choice, mirrors _SPEED_BRACKET_MAX_DOUBLINGS."""


def _solve_speeds_vectorized(
    target_power_w: np.ndarray,
    *,
    gradient: float,
    cda: np.ndarray,
    crr: np.ndarray,
    mass_kg: float,
    air_density: float,
    drivetrain_efficiency: float = DEFAULT_DRIVETRAIN_EFFICIENCY,
) -> np.ndarray:
    """Vectorized numeric solver: speeds (m/s) for arrays of target powers.

    The same Martin et al. 1998 balance as :func:`required_bike_power`, in
    still air, solved simultaneously for a whole Monte Carlo sample vector by
    array bisection (the residual ``A v^3 + (B + m g sin(theta)) v - eta*P``
    has exactly one positive root for every gradient > -1: even on a descent
    steep enough to make ``B + m g sin(theta)`` negative the residual only
    dips before rising strictly monotonically past its minimum, crossing zero
    once). Used by the composite MC path; at nominal inputs it agrees with
    the scalar :func:`bike_speed_for_power` to solver tolerance (asserted by
    the tests).

    Callers must pre-validate the scalar parameters (gradient > -1, positive
    mass/air density/efficiency); ``target_power_w`` entries must be positive
    (``ValueError``).
    """
    if np.any(np.asarray(target_power_w) <= 0.0):
        raise ValueError("every target power must be positive")
    theta = math.atan(gradient)
    big_a = 0.5 * air_density * cda
    big_b = crr * (mass_kg * GRAVITY_M_S2 * math.cos(theta)) + (
        mass_kg * GRAVITY_M_S2 * math.sin(theta)
    )
    big_c = np.asarray(target_power_w, dtype=float) * drivetrain_efficiency
    lo = np.zeros_like(big_c)
    hi = np.ones_like(big_c)
    for _ in range(_MC_VECTOR_MAX_DOUBLINGS):
        needs = (big_a * hi**3 + big_b * hi) < big_c
        if not bool(needs.any()):
            break
        hi = np.where(needs, hi * 2.0, hi)
    else:
        raise ValueError(
            "speed bracket exhausted in the Monte Carlo solver: target power "
            "unreachable with the given inputs"
        )
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        below = (big_a * mid**3 + big_b * mid) < big_c
        lo = np.where(below, mid, lo)
        hi = np.where(below, hi, mid)
        if float((hi - lo).max()) < _MC_VECTOR_SPEED_TOLERANCE_MPS:
            break
    return 0.5 * (lo + hi)


@dataclass(frozen=True, slots=True)
class TriathlonPrediction:
    """Composite triathlon prediction: every segment plus the total, each a
    full envelope (seconds). Frozen and slotted, consistent with the other
    engine result conventions."""

    swim: PredictionEnvelope
    bike: BikeCoursePrediction
    run: PredictionEnvelope
    transitions: TransitionsPrediction
    total: PredictionEnvelope


def _mc_band(
    samples: np.ndarray,
    point: float,
    method: str,
    assumptions: tuple[str, ...],
) -> PredictionEnvelope:
    """Envelope from Monte Carlo samples: point = the deterministic nominal,
    band = the 2.5/97.5 percentiles, widened to contain the point (documented
    guarantee: the point estimate never falls outside its own band).
    """
    lower = float(np.percentile(samples, MONTE_CARLO_LOWER_PERCENTILE))
    upper = float(np.percentile(samples, MONTE_CARLO_UPPER_PERCENTILE))
    return PredictionEnvelope(
        point=point,
        lower=min(lower, point),
        upper=max(upper, point),
        method=method,
        assumptions=assumptions,
        model_version=PREDICTION_MODEL_VERSION,
    )


def _rel_sigma_from_band(envelope: PredictionEnvelope) -> float:
    """Relative sigma implied by a +/- 2-sigma band (band half-width / 4)."""
    return (envelope.upper - envelope.lower) / (4.0 * envelope.point)


def triathlon_total_prediction(
    *,
    reference_run_distance_m: float,
    reference_run_time_s: float,
    run_distance_m: float,
    swim_distance_m: float,
    css_mps: float,
    bike_segments: Sequence[tuple[float, float]],
    ftp_watts: float,
    cda: float,
    crr: float,
    total_mass_kg: float,
    temperature_c: float,
    altitude_m: float,
    brick_factor_observations: Sequence[float] = (),
    past_t1_s: Sequence[float] = (),
    past_t2_s: Sequence[float] = (),
    race_key: str | None = None,
    open_water_factor: float = DEFAULT_OPEN_WATER_FACTOR,
    drivetrain_efficiency: float = DEFAULT_DRIVETRAIN_EFFICIENCY,
    n_samples: int = DEFAULT_MONTE_CARLO_SAMPLES,
    seed: int = DEFAULT_MONTE_CARLO_SEED,
) -> TriathlonPrediction:
    """Composite triathlon prediction with Monte Carlo uncertainty, RP-8.

    The five parts (swim, bike per segment, run off the bike, T1, T2) are
    predicted at nominal inputs exactly as in their standalone functions;
    uncertainty is propagated by Monte Carlo over the DOCUMENTED input
    distributions (section 7.7 — distributions are documented assumptions,
    every sigma an OWNER CHOICE module constant):

    - swim speed: normal, +/- :data:`SWIM_CSS_REL_SIGMA` (0.02) per sample;
    - bike: target power fraction x normal(+/- :data:`BIKE_POWER_REL_SIGMA`),
      CdA x normal(+/- :data:`CDA_REL_SIGMA`), Crr x
      normal(+/- :data:`CRR_REL_SIGMA`), solved per segment with the
      vectorized Martin et al. 1998 solver;
    - run: Riegel exponent ~ uniform over 1.06 +/-
      :data:`RIEGEL_EXPONENT_UNCERTAINTY`; the run-off-the-bike factor ~
      normal around its envelope point with the sigma implied by its band
      (widenend prior without personal data), clipped at >= 1 (a brick run is
      never FASTER than the same run raced fresh — documented assumption);
    - transitions: normal around the transition envelope points with the
      sigma implied by their bands, clipped positive.

    Determinism: the generator is ``numpy.random.default_rng(seed)`` with a
    FIXED default seed (:data:`DEFAULT_MONTE_CARLO_SEED`), so the segment
    and total bounds are stable across calls with the same inputs (RP-8
    acceptance, asserted exactly by the tests).

    Point estimates are the deterministic physics points (never the MC mean);
    lower/upper are the :data:`MONTE_CARLO_LOWER_PERCENTILE` /
    :data:`MONTE_CARLO_UPPER_PERCENTILE` percentiles of the samples, widened
    to contain the point. ``n_samples`` must be positive (``ValueError``);
    segment and athlete inputs validated like the standalone functions.
    """
    if n_samples <= 0:
        raise ValueError(f"n_samples must be positive, got {n_samples!r}")
    if ftp_watts <= 0.0:
        raise ValueError(f"FTP must be positive, got {ftp_watts!r} W")
    if not bike_segments:
        raise ValueError("bike_segments must not be empty: no course was given")
    if any(gradient <= -1.0 for _, gradient in bike_segments):
        raise ValueError("every segment gradient (rise/run) must be greater than -1")
    rng = np.random.default_rng(seed)

    # --- Swim (nominal point from the standalone model, MC on the speed) ---
    swim_point_envelope = swim_prediction(
        swim_distance_m, css_mps, open_water_factor=open_water_factor
    )
    swim_samples = swim_point_envelope.point / rng.normal(
        1.0, SWIM_CSS_REL_SIGMA, n_samples
    )
    swim_envelope = _mc_band(
        swim_samples,
        swim_point_envelope.point,
        "css_fraction_open_water_monte_carlo",
        (
            *swim_point_envelope.assumptions,
            "MC speed distribution: normal +/- 2% relative (OWNER CHOICE)",
        ),
    )

    # --- Bike (nominal points from the scalar solver, MC on power/CdA/Crr) ---
    total_bike_distance = math.fsum(distance for distance, _ in bike_segments)
    power_fraction = default_bike_power_fraction(total_bike_distance)
    nominal_power = ftp_watts * power_fraction
    rho = air_density(temperature_c, altitude_m)
    power_samples = nominal_power * rng.normal(1.0, BIKE_POWER_REL_SIGMA, n_samples)
    cda_samples = cda * rng.normal(1.0, CDA_REL_SIGMA, n_samples)
    crr_samples = crr * rng.normal(1.0, CRR_REL_SIGMA, n_samples)
    bike_segment_envelopes: list[PredictionEnvelope] = []
    bike_total_samples = np.zeros(n_samples)
    for distance, gradient in bike_segments:
        if distance <= 0.0:
            raise ValueError(f"segment distance must be positive, got {distance!r} m")
        nominal_speed = bike_speed_for_power(
            nominal_power,
            gradient=gradient,
            cda=cda,
            crr=crr,
            mass_kg=total_mass_kg,
            air_density=rho,
            drivetrain_efficiency=drivetrain_efficiency,
        )
        nominal_time = distance / nominal_speed
        speeds = _solve_speeds_vectorized(
            power_samples,
            gradient=gradient,
            cda=cda_samples,
            crr=crr_samples,
            mass_kg=total_mass_kg,
            air_density=rho,
            drivetrain_efficiency=drivetrain_efficiency,
        )
        segment_samples = distance / speeds
        bike_total_samples = bike_total_samples + segment_samples
        bike_segment_envelopes.append(
            _mc_band(
                segment_samples,
                nominal_time,
                "martin_1998_monte_carlo",
                (
                    "P*eta = 0.5*rho*CdA*v_air^2*v + Crr*m*g*cos(theta)*v + "
                    "m*g*sin(theta)*v solved per Monte Carlo sample "
                    "(Martin et al. 1998)",
                    "MC input distributions (documented assumptions, OWNER "
                    "CHOICE): power +/- 3%, CdA +/- 3%, Crr +/- 10% relative",
                ),
            )
        )
    bike_total_point = math.fsum(e.point for e in bike_segment_envelopes)
    bike_total = _mc_band(
        bike_total_samples,
        bike_total_point,
        "martin_1998_monte_carlo_course_sum",
        (
            "sum of the per-segment Monte Carlo solutions",
            "MC input distributions: as the per-segment bike assumptions",
        ),
    )

    # --- Run off the bike (Riegel exponent + factor distributions) ---
    run_prediction = predict_run(reference_run_distance_m, reference_run_time_s, run_distance_m)
    standalone_point = run_prediction.riegel.point
    exponent_samples = rng.uniform(
        RIEGEL_EXPONENT - _RUN_EXPONENT_SAMPLING_UNCERTAINTY,
        RIEGEL_EXPONENT + _RUN_EXPONENT_SAMPLING_UNCERTAINTY,
        n_samples,
    )
    standalone_samples = reference_run_time_s * (
        run_distance_m / reference_run_distance_m
    ) ** exponent_samples
    factor_envelope = run_off_bike_factor(brick_factor_observations)
    factor_sigma = _rel_sigma_from_band(factor_envelope)
    factor_samples = np.clip(
        rng.normal(factor_envelope.point, factor_sigma * factor_envelope.point, n_samples),
        1.0,
        None,
    )
    run_samples = standalone_samples * factor_samples
    run_envelope = _mc_band(
        run_samples,
        standalone_point * factor_envelope.point,
        "riegel_run_off_bike_monte_carlo",
        (
            *run_prediction.riegel.assumptions,
            *factor_envelope.assumptions,
            "MC distributions: exponent ~ uniform over the documented range; "
            "factor ~ normal (clipped at 1: a brick run is never faster than "
            "the same run raced fresh — documented assumption)",
        ),
    )

    # --- Transitions ---
    transitions = transition_prediction(
        past_t1_s, past_t2_s, race_key=race_key
    )
    t1_samples = np.clip(
        rng.normal(
            transitions.t1.point,
            _rel_sigma_from_band(transitions.t1) * transitions.t1.point,
            n_samples,
        ),
        1e-6,
        None,
    )
    t2_samples = np.clip(
        rng.normal(
            transitions.t2.point,
            _rel_sigma_from_band(transitions.t2) * transitions.t2.point,
            n_samples,
        ),
        1e-6,
        None,
    )
    t1_envelope = _mc_band(
        t1_samples,
        transitions.t1.point,
        "transition_monte_carlo",
        (
            *transitions.t1.assumptions,
            "MC distribution: normal around the transition point with the "
            "sigma implied by its band, clipped positive",
        ),
    )
    t2_envelope = _mc_band(
        t2_samples,
        transitions.t2.point,
        "transition_monte_carlo",
        (
            *transitions.t2.assumptions,
            "MC distribution: normal around the transition point with the "
            "sigma implied by its band, clipped positive",
        ),
    )

    # --- Total ---
    total_point = (
        swim_envelope.point
        + bike_total.point
        + run_envelope.point
        + t1_envelope.point
        + t2_envelope.point
    )
    total_samples = swim_samples + bike_total_samples + run_samples + t1_samples + t2_samples
    total_envelope = _mc_band(
        total_samples,
        total_point,
        "monte_carlo_composite",
        (
            "total = swim + bike + run-off-the-bike + T1 + T2, propagated by "
            "Monte Carlo over the documented input distributions (section 7.7)",
            f"rng = numpy default_rng, seed {seed} (FIXED default, RP-8); "
            f"{n_samples} samples; band = 2.5/97.5 percentiles (OWNER CHOICE)",
        ),
    )
    return TriathlonPrediction(
        swim=swim_envelope,
        bike=BikeCoursePrediction(
            segments=tuple(bike_segment_envelopes), total=bike_total
        ),
        run=run_envelope,
        transitions=TransitionsPrediction(t1=t1_envelope, t2=t2_envelope),
        total=total_envelope,
    )


