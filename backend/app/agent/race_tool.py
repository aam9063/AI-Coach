"""Data assembly for the ``predict_race`` tool (RP-10, PROJECT_BRIEF §9.3/§7.7).

The tool wrapper lives in :mod:`app.agent.tools` (argument validation —
exactly one of ``race_id``/``distance`` — the ``target_date`` parsing and
the fail-safe error shapes); THIS module holds what the wrapper delegates:
the typed result models and the two per-path assemblies, following the
:mod:`app.agent.chart_tool` pattern. It imports only the pure engine, the
DB models and the settings type — never :mod:`app.agent.tools` (no import
cycle).

Every number returned is an ENGINE output: the assemblies resolve inputs
and call :func:`app.engine.predict.triathlon_total_prediction`; they never
compute a time themselves (§3). Each segment and the total travel as the
engine's FULL :class:`~app.engine.predict.PredictionEnvelope`
``{point, lower, upper, method, assumptions, model_version}`` with every
field VERBATIM — the assumptions are never stripped or paraphrased (§7.7).

Two paths (§9.3 ``predict_race(race_id | distance, target_date?)``):

- **race_id path**: load the :class:`~app.db.models.RaceRow` course
  (gradient segments, expected temperature, altitude) and transitions
  (past T1/T2 observations), resolve the athlete inputs, call the engine,
  PERSIST a :class:`~app.db.models.PredictionRow` and return the typed
  result. This is the only path that persists: the row references the
  race it was computed for.
- **distance path**: no race row exists, so there is nothing to load and
  NOTHING TO PERSIST — a ``PredictionRow`` needs a ``race_id`` to be
  auditable back to a course (documented RP-10 decision; the row's
  ``race_id`` is kept nullable for future callers, but this tool never
  writes an orphan prediction). The prediction runs on the documented
  standard distances for the race key over a single flat segment at the
  ISA standard-atmosphere defaults, and those defaults are stated in the
  result's coverage so the answer is never mistaken for course data.

Athlete inputs (both paths) come from the OWNER CONFIGURATION — exactly
the ``ATHLETE_*`` settings the zone tools read:

- ``ATHLETE_FTP_W`` — bike target power is a fraction of FTP (engine
  ladder, OWNER CHOICE per distance);
- ``ATHLETE_CSS_SPEED_MPS`` — the swim model's CSS of record;
- ``ATHLETE_THRESHOLD_RUN_SPEED_MPS`` — the run model needs a reference
  effort; the documented derivation is the threshold speed held over
  :data:`REFERENCE_RUN_DISTANCE_M` (5000 m at threshold pace, OWNER
  CHOICE: threshold pace is the athlete's own measured input, so the
  reference effort is derived, never invented);
- ``ATHLETE_WEIGHT_KG`` — the rider mass when configured; otherwise the
  documented :data:`DEFAULT_RIDER_MASS_KG` (provenance reported in the
  coverage either way).

A missing FTP/CSS/run threshold is an ``insufficient_data`` result naming
exactly which variable is missing — inputs are NEVER guessed (§7.7/§9.3).

Documented OWNER CHOICE defaults for inputs the configuration does not
carry (stated in the coverage, never buried): :data:`DEFAULT_CDA_M2`,
:data:`DEFAULT_CRR`, :data:`DEFAULT_BIKE_MASS_KG`,
:data:`DEFAULT_TEMPERATURE_C`, :data:`DEFAULT_ALTITUDE_M`.

The ``race.transitions`` JSONB supports the shapes the ENGINE's composite
API accepts: past observations (``past_t1_s``/``past_t2_s``). A per-race
T1/T2 override is stored on the race row but the composite engine call has
no override input, so the tool answers with an explicit error rather than
silently ignoring stored data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import PredictionRow, RaceRow
from app.engine.predict import (
    DEFAULT_TRANSITIONS_S,
    PredictionEnvelope,
    TriathlonPrediction,
    triathlon_total_prediction,
)

RACE_DISTANCE_KEYS: Final[tuple[str, ...]] = tuple(sorted(DEFAULT_TRANSITIONS_S))
"""The accepted distance keys, DERIVED from the engine's transition
defaults vocabulary (identity, not a copy) — a new engine race key is
accepted here automatically."""

STANDARD_SEGMENTS_M: Final[dict[str, tuple[float, float, float]]] = {
    # race_key -> (swim_m, bike_m, run_m): the standard triathlon
    # distances. OWNER CHOICE (the engine's race-key vocabulary; the long
    # runs are the exact half-marathon/marathon lengths).
    "sprint": (750.0, 20000.0, 5000.0),
    "olympic": (1500.0, 40000.0, 10000.0),
    "half": (1900.0, 90000.0, 21097.5),
    "full": (3800.0, 180000.0, 42195.0),
}

REFERENCE_RUN_DISTANCE_M: Final[float] = 5000.0
"""Reference distance of the derived run reference effort (metres).

OWNER CHOICE: the threshold run speed is a measured athlete input; the
reference effort handed to :func:`app.engine.predict.predict_run` is that
speed held over 5000 m (``reference_time = 5000 / threshold_speed``), a
documented derivation — never an invented effort.
"""

DEFAULT_CDA_M2: Final[float] = 0.30
"""Default drag area CdA (m^2) when the race row carries no equipment
data. OWNER CHOICE (a typical road/Tri bar setup; 0.20-0.35 is the usual
age-group range)."""

DEFAULT_CRR: Final[float] = 0.005
"""Default rolling-resistance coefficient Crr. OWNER CHOICE (asphalt with
road tyres; the commonly cited 0.004-0.006 band)."""

DEFAULT_RIDER_MASS_KG: Final[float] = 75.0
"""Default rider mass (kg) when ``ATHLETE_WEIGHT_KG`` is unconfigured.
OWNER CHOICE (documented default, provenance reported in the coverage)."""

DEFAULT_BIKE_MASS_KG: Final[float] = 10.0
"""Mass added for bike + kit (kg) on top of the rider mass. OWNER CHOICE
(a road/triathlon bike with bottles and shoes)."""

DEFAULT_TEMPERATURE_C: Final[float] = 15.0
"""Default air temperature (°C): the ISA standard atmosphere sea-level
temperature — LITERATURE (ISO 2533, the engine's own ``air_density``
anchor) — used when the race row states no expected temperature."""

DEFAULT_ALTITUDE_M: Final[float] = 0.0
"""Default altitude (m, sea level; ISA validity requires 0-11000 m) when
the race row states none."""


# ---------------------------------------------------------------------------
# Typed result models (the PlotMetricResult pattern)
# ---------------------------------------------------------------------------


class PredictionEnvelopeOut(BaseModel):
    """The engine's prediction envelope, field-for-field VERBATIM (§7.7).

    ``assumptions`` is the engine's own assumption list, never stripped or
    paraphrased; ``model_version`` is the prediction model's version
    (``app.engine.predict.PREDICTION_MODEL_VERSION``). Times are seconds.
    """

    point: float
    lower: float
    upper: float
    method: str
    assumptions: list[str]
    model_version: str


class SegmentPredictionOut(PredictionEnvelopeOut):
    """One named segment's envelope: ``swim``, ``bike_N`` (one per course
    segment), ``run`` (off the bike), ``t1``, ``t2``."""

    segment: str


class RacePredictionCoverage(BaseModel):
    """What ACTUALLY backed the prediction — truthful, never padded.

    ``course_segments`` is the number of course-profile segments consumed
    (``0`` = the documented standard flat course was assumed);
    ``athlete_inputs`` maps each input name to its provenance (owner
    configuration env var vs documented default); ``run_anchor`` names
    the derived reference effort the run prediction used — 5000 m at
    the owner-configured threshold run speed — and the env var it came
    from (the honest limit: the anchor is a derivation, never a
    measured race effort); ``transitions`` states where T1/T2 came
    from; ``persisted``/``prediction_id`` report whether a
    :class:`~app.db.models.PredictionRow` was written (race_id path
    only — the distance path never persists).
    """

    path: Literal["race_id", "distance"]
    course_segments: int | None = None
    temperature_c: float | None = None
    altitude_m: float | None = None
    athlete_inputs: dict[str, str]
    run_anchor: str | None = None
    transitions: str
    persisted: bool
    prediction_id: int | None = None


class RacePredictionResult(BaseModel):
    """The typed ``predict_race`` result (RP-10, §9.3): per-segment and
    total envelopes, the WA-6 provenance contract (``engine_version``,
    ``computed_at``, ``coverage``) and the fail-safe statuses.

    On ``error``/``insufficient_data`` the prediction fields stay unset;
    the tool dumps the model with ``exclude_none=True`` so a failed call
    carries no prediction keys at all — nothing is ever fabricated."""

    status: Literal["ok", "insufficient_data", "error"]
    race_key: str | None = None
    target_date: str | None = None
    detail: str | None = None
    segments: list[SegmentPredictionOut] | None = None
    total: PredictionEnvelopeOut | None = None
    coverage: RacePredictionCoverage | None = None
    engine_version: str
    computed_at: str


# ---------------------------------------------------------------------------
# Input resolution (owner configuration; never guessed)
# ---------------------------------------------------------------------------

_ATHLETE_INPUTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("ftp_watts", "athlete_ftp_w", "ATHLETE_FTP_W"),
    ("css_mps", "athlete_css_speed_mps", "ATHLETE_CSS_SPEED_MPS"),
    (
        "threshold_run_speed_mps",
        "athlete_threshold_run_speed_mps",
        "ATHLETE_THRESHOLD_RUN_SPEED_MPS",
    ),
)
"""(name, Settings field, env var) of the REQUIRED athlete inputs, the
same owner-configuration fields the zone tools read."""


class _AthleteInputs:
    """The resolved athlete inputs plus per-input provenance."""

    def __init__(self, settings: Settings) -> None:
        self.values: dict[str, float] = {}
        self.provenance: dict[str, str] = {}
        self.missing: list[str] = []
        for name, settings_field, env_var in _ATHLETE_INPUTS:
            value = getattr(settings, settings_field)
            if value is None:
                self.missing.append(env_var)
            else:
                self.values[name] = float(value)
                self.provenance[name] = f"owner configuration ({env_var})"
        # Optional-with-documented-default inputs: the coverage reports the
        # provenance either way, so a default is never mistaken for data.
        self.values["rider_mass_kg"] = (
            float(settings.athlete_weight_kg)
            if settings.athlete_weight_kg is not None
            else DEFAULT_RIDER_MASS_KG
        )
        self.provenance["rider_mass_kg"] = (
            "owner configuration (ATHLETE_WEIGHT_KG)"
            if settings.athlete_weight_kg is not None
            else f"default (OWNER CHOICE: {DEFAULT_RIDER_MASS_KG} kg rider)"
        )
        self.values["cda_m2"] = DEFAULT_CDA_M2
        self.provenance["cda_m2"] = f"default (OWNER CHOICE: {DEFAULT_CDA_M2} m^2)"
        self.values["crr"] = DEFAULT_CRR
        self.provenance["crr"] = f"default (OWNER CHOICE: {DEFAULT_CRR})"


def _insufficient_detail(missing: list[str]) -> str:
    """The detail naming EXACTLY which owner-configuration is missing."""
    joined = ", ".join(missing)
    return (
        f"no race prediction is possible without the athlete's thresholds: "
        f"missing {joined}. Set "
        f"{', '.join(missing)} in the owner configuration (.env — the same "
        "variables the zone tools read), then ask again. Missing inputs are "
        "never guessed (§7.7/§9.3)."
    )


def _run_anchor(athlete: _AthleteInputs) -> str:
    """The run anchor effort and its origin, for the coverage (the honest
    limit of the run prediction, RP-1/RP-2).

    The anchor is a DERIVATION, never a measured race effort: the
    threshold run speed held over :data:`REFERENCE_RUN_DISTANCE_M`
    (``reference_time = 5000 / threshold_speed``). The provenance string
    names the owner-configuration env var the speed came from, so the
    athlete can always trace where the anchor was built from."""
    speed = athlete.values["threshold_run_speed_mps"]
    reference_time_s = REFERENCE_RUN_DISTANCE_M / speed
    return (
        f"derived reference effort: {REFERENCE_RUN_DISTANCE_M:g} m at the "
        f"threshold run speed {speed} m/s = {reference_time_s:.1f} s "
        f"({athlete.provenance['threshold_run_speed_mps']}) — the anchor "
        "the run prediction extrapolates from (Riegel/VDOT), not a "
        "measured race effort"
    )


# ---------------------------------------------------------------------------
# Engine call + envelope conversion
# ---------------------------------------------------------------------------


def _envelope_fields(envelope: PredictionEnvelope) -> dict[str, Any]:
    """The six contract fields, VERBATIM (assumptions as a list copy)."""
    return {
        "point": envelope.point,
        "lower": envelope.lower,
        "upper": envelope.upper,
        "method": envelope.method,
        "assumptions": list(envelope.assumptions),
        "model_version": envelope.model_version,
    }


def _segment_outs(prediction: TriathlonPrediction) -> list[SegmentPredictionOut]:
    """The named segments in stable order: swim, bike_N..., run, t1, t2."""
    segments = [
        SegmentPredictionOut(segment="swim", **_envelope_fields(prediction.swim))
    ]
    segments.extend(
        SegmentPredictionOut(segment=f"bike_{n}", **_envelope_fields(envelope))
        for n, envelope in enumerate(prediction.bike.segments, start=1)
    )
    segments.append(SegmentPredictionOut(segment="run", **_envelope_fields(prediction.run)))
    segments.append(
        SegmentPredictionOut(segment="t1", **_envelope_fields(prediction.transitions.t1))
    )
    segments.append(
        SegmentPredictionOut(segment="t2", **_envelope_fields(prediction.transitions.t2))
    )
    return segments


def _total_out(prediction: TriathlonPrediction) -> PredictionEnvelopeOut:
    return PredictionEnvelopeOut(**_envelope_fields(prediction.total))


def _result(
    *,
    status: Literal["ok", "insufficient_data", "error"],
    race_key: str | None,
    target_date: date | None,
    settings: Settings,
    computed_at: datetime,
    detail: str | None = None,
    prediction: TriathlonPrediction | None = None,
    coverage: RacePredictionCoverage | None = None,
) -> RacePredictionResult:
    return RacePredictionResult(
        status=status,
        race_key=race_key,
        target_date=target_date.isoformat() if target_date is not None else None,
        detail=detail,
        segments=_segment_outs(prediction) if prediction is not None else None,
        total=_total_out(prediction) if prediction is not None else None,
        coverage=coverage,
        engine_version=settings.engine_version,
        computed_at=computed_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Shared engine call over resolved inputs
# ---------------------------------------------------------------------------


def _run_engine(
    *,
    athlete: _AthleteInputs,
    bike_segments: list[tuple[float, float]],
    swim_distance_m: float,
    run_distance_m: float,
    race_key: str | None,
    temperature_c: float,
    altitude_m: float,
    past_t1_s: Sequence[float] = (),
    past_t2_s: Sequence[float] = (),
) -> TriathlonPrediction:
    """One engine composite call over fully resolved, validated inputs.

    The run reference effort is the documented derivation
    (:data:`REFERENCE_RUN_DISTANCE_M` at the threshold run speed). No
    brick-run observations are assembled yet — the engine's configurable
    prior with its WIDENED band applies (RP-6), stated in the run
    envelope's own assumptions."""
    return triathlon_total_prediction(
        reference_run_distance_m=REFERENCE_RUN_DISTANCE_M,
        reference_run_time_s=(
            REFERENCE_RUN_DISTANCE_M / athlete.values["threshold_run_speed_mps"]
        ),
        run_distance_m=run_distance_m,
        swim_distance_m=swim_distance_m,
        bike_segments=bike_segments,
        ftp_watts=athlete.values["ftp_watts"],
        css_mps=athlete.values["css_mps"],
        cda=athlete.values["cda_m2"],
        crr=athlete.values["crr"],
        total_mass_kg=(
            athlete.values["rider_mass_kg"] + DEFAULT_BIKE_MASS_KG
        ),
        temperature_c=temperature_c,
        altitude_m=altitude_m,
        brick_factor_observations=(),
        past_t1_s=past_t1_s,
        past_t2_s=past_t2_s,
        race_key=race_key,
    )


# ---------------------------------------------------------------------------
# race_id path: course data from the race row, prediction PERSISTED
# ---------------------------------------------------------------------------


def _parse_course(
    course: dict[str, Any],
) -> tuple[list[tuple[float, float]], float, float]:
    """``(bike_segments, temperature_c, altitude_m)`` from the course JSONB.

    Shape (documented on :class:`RaceRow`): ``segments`` is a list of
    ``{"distance_m": ..., "gradient": ...}``; ``temperature_c`` and
    ``altitude_m`` fall back to the documented ISA defaults when absent.
    """
    segments = [
        (float(entry["distance_m"]), float(entry["gradient"]))
        for entry in course["segments"]
    ]
    temperature = float(course.get("temperature_c", DEFAULT_TEMPERATURE_C))
    altitude = float(course.get("altitude_m", DEFAULT_ALTITUDE_M))
    return segments, temperature, altitude


def _transitions_from_row(
    transitions: dict[str, Any] | None, race_key: str
) -> tuple[list[float], list[float], str] | None:
    """``(past_t1_s, past_t2_s, coverage_note)`` from the transitions JSONB.

    ``None`` means the stored shape cannot be honored: a per-race
    T1/T2 OVERRIDE is data the composite engine API does not accept, and
    silently ignoring stored data would be a lie — the caller answers with
    an explicit error instead.
    """
    if transitions is None:
        if race_key in DEFAULT_TRANSITIONS_S:
            return [], [], f"defaults for '{race_key}'"
        return [], [], "generic defaults (custom race key)"
    if "t1_override_s" in transitions or "t2_override_s" in transitions:
        return None
    past_t1 = [float(v) for v in transitions.get("past_t1_s", [])]
    past_t2 = [float(v) for v in transitions.get("past_t2_s", [])]
    if past_t1 or past_t2:
        note = f"past data ({len(past_t1)} T1 / {len(past_t2)} T2 observations)"
    elif race_key in DEFAULT_TRANSITIONS_S:
        note = f"defaults for '{race_key}'"
    else:
        note = "generic defaults (custom race key)"
    return past_t1, past_t2, note


async def _predict_from_race_row(
    session: AsyncSession,
    race_id: int,
    target_date: date | None,
    settings: Settings,
    computed_at: datetime,
) -> RacePredictionResult:
    """Load the race row, call the engine, persist a PredictionRow."""
    row = await session.get(RaceRow, race_id)
    if row is None:
        return _result(
            status="error",
            race_key=None,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=(
                f"no race with id {race_id} is registered in the race table. "
                "Ask the owner to seed the target race (race_key, course "
                "profile, expected temperature, goal) before predicting it — "
                "course data is never invented."
            ),
        )
    athlete = _AthleteInputs(settings)
    if athlete.missing:
        return _result(
            status="insufficient_data",
            race_key=row.race_key,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=_insufficient_detail(athlete.missing),
            coverage=RacePredictionCoverage(
                path="race_id",
                athlete_inputs=athlete.provenance,
                transitions="not reached: athlete inputs missing",
                persisted=False,
            ),
        )
    # Swim/run split: the standard distances for a known race key; a
    # custom key must carry them in the course JSONB — never guessed.
    if row.race_key in STANDARD_SEGMENTS_M:
        swim_m, _, run_m = STANDARD_SEGMENTS_M[row.race_key]
    elif row.course is not None and "swim_distance_m" in row.course and (
        "run_distance_m" in row.course
    ):
        swim_m = float(row.course["swim_distance_m"])
        run_m = float(row.course["run_distance_m"])
    else:
        return _result(
            status="error",
            race_key=row.race_key,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=(
                f"race {row.race_key!r} (id {race_id}) has a custom race_key "
                "with no standard swim/run split: seed swim_distance_m and "
                "run_distance_m in the race row's course JSONB — the split "
                "is never guessed."
            ),
        )
    # Bike profile: the course segments when seeded, else the documented
    # standard flat course for a known key.
    if row.course is not None and "segments" in row.course:
        bike_segments, temperature_c, altitude_m = _parse_course(row.course)
        course_segments = len(bike_segments)
    elif row.race_key in STANDARD_SEGMENTS_M:
        bike_segments = [(STANDARD_SEGMENTS_M[row.race_key][1], 0.0)]
        temperature_c, altitude_m = DEFAULT_TEMPERATURE_C, DEFAULT_ALTITUDE_M
        course_segments = 0
    else:
        return _result(
            status="error",
            race_key=row.race_key,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=(
                f"race {row.race_key!r} (id {race_id}) has no course bike "
                "segments: seed the race row's course segments — never "
                "guess a course."
            ),
        )
    transitions = _transitions_from_row(row.transitions, row.race_key)
    if transitions is None:
        return _result(
            status="error",
            race_key=row.race_key,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=(
                f"race {row.race_key!r} (id {race_id}) stores per-race T1/T2 "
                "overrides, but the prediction engine's composite API accepts "
                "only past observations or its documented defaults — overrides "
                "are never silently ignored. Store past_t1_s/past_t2_s "
                "observations instead, or remove the override."
            ),
        )
    past_t1_s, past_t2_s, transitions_note = transitions
    prediction = _run_engine(
        athlete=athlete,
        bike_segments=bike_segments,
        swim_distance_m=swim_m,
        run_distance_m=run_m,
        race_key=row.race_key if row.race_key in DEFAULT_TRANSITIONS_S else None,
        temperature_c=temperature_c,
        altitude_m=altitude_m,
        past_t1_s=past_t1_s,
        past_t2_s=past_t2_s,
    )
    # Persist the FULL envelopes verbatim (§7.7) and report the row id.
    stored = PredictionRow(
        race_id=row.id,
        race_key=row.race_key,
        target_date=target_date,
        segments=[s.model_dump() for s in _segment_outs(prediction)],
        total=_envelope_fields(prediction.total),
        engine_version=settings.engine_version,
    )
    session.add(stored)
    await session.commit()
    return _result(
        status="ok",
        race_key=row.race_key,
        target_date=target_date,
        settings=settings,
        computed_at=computed_at,
        prediction=prediction,
        coverage=RacePredictionCoverage(
            path="race_id",
            course_segments=course_segments,
            temperature_c=temperature_c,
            altitude_m=altitude_m,
            athlete_inputs=athlete.provenance,
            run_anchor=_run_anchor(athlete),
            transitions=transitions_note,
            persisted=True,
            prediction_id=stored.id,
        ),
    )


# ---------------------------------------------------------------------------
# distance path: standard flat course, NO persistence (no race row)
# ---------------------------------------------------------------------------


def _predict_from_distance(
    distance: str,
    target_date: date | None,
    settings: Settings,
    computed_at: datetime,
) -> RacePredictionResult:
    """Standard-distance prediction; returns WITHOUT persisting (there is
    no race row to reference — documented in the module docstring)."""
    # Defensive str() coercion: a caller that bypassed the tool's string
    # schema (e.g. a numeric distance) gets an explicit unknown-key error
    # response instead of an exception into the agent loop.
    key = str(distance).strip().lower()
    if key not in STANDARD_SEGMENTS_M:
        return _result(
            status="error",
            race_key=None,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=(
                f"unknown race distance {distance!r}: expected one of "
                f"{list(RACE_DISTANCE_KEYS)}. For a course-specific "
                "prediction, seed the race row and call with race_id."
            ),
        )
    athlete = _AthleteInputs(settings)
    if athlete.missing:
        return _result(
            status="insufficient_data",
            race_key=key,
            target_date=target_date,
            settings=settings,
            computed_at=computed_at,
            detail=_insufficient_detail(athlete.missing),
            coverage=RacePredictionCoverage(
                path="distance",
                athlete_inputs=athlete.provenance,
                transitions=f"defaults for '{key}'",
                persisted=False,
            ),
        )
    swim_m, bike_m, run_m = STANDARD_SEGMENTS_M[key]
    prediction = _run_engine(
        athlete=athlete,
        # No course data for a distance-only prediction: one flat segment
        # at the ISA defaults — documented assumptions, stated in the
        # envelopes themselves and the coverage.
        bike_segments=[(bike_m, 0.0)],
        swim_distance_m=swim_m,
        run_distance_m=run_m,
        race_key=key,
        temperature_c=DEFAULT_TEMPERATURE_C,
        altitude_m=DEFAULT_ALTITUDE_M,
    )
    return _result(
        status="ok",
        race_key=key,
        target_date=target_date,
        settings=settings,
        computed_at=computed_at,
        prediction=prediction,
        coverage=RacePredictionCoverage(
            path="distance",
            course_segments=0,
            temperature_c=DEFAULT_TEMPERATURE_C,
            altitude_m=DEFAULT_ALTITUDE_M,
            athlete_inputs=athlete.provenance,
            run_anchor=_run_anchor(athlete),
            transitions=f"defaults for '{key}'",
            persisted=False,
        ),
    )


# ---------------------------------------------------------------------------
# Entry point (the tool wrapper delegates here)
# ---------------------------------------------------------------------------


async def predict_race(
    session: AsyncSession,
    *,
    race_id: int | None,
    distance: str | None,
    target_date: date | None,
    settings: Settings,
    computed_at: datetime,
) -> RacePredictionResult:
    """Assemble the ``predict_race`` result for exactly one path.

    The wrapper has already validated "exactly one of race_id/distance"
    and parsed ``target_date``. The race_id path reads the race row and
    PERSISTS the prediction; the distance path never touches the session
    and never persists (documented RP-10 decision).
    """
    if race_id is not None:
        return await _predict_from_race_row(
            session, race_id, target_date, settings, computed_at
        )
    assert distance is not None  # the wrapper validated exactly-one
    return _predict_from_distance(distance, target_date, settings, computed_at)
