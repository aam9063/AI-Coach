"""RP-10 tests: the ``predict_race`` tool (§9.3, Feature 8 race-prediction).

Contract pinned here:

- **Registry**: ``predict_race`` is a member of the explicit
  ``tool_list()`` (never a loader).
- **Argument errors** (never exceptions into the agent loop, never
  fabricated predictions): neither or both of ``race_id``/``distance``, an
  unknown ``distance`` key (the accepted set is the ENGINE's race-key
  vocabulary, ``app.engine.predict.DEFAULT_TRANSITIONS_S``), and an
  unparseable ``target_date`` are ``status="error"`` responses.
- **Distance path** (no race row): the athlete inputs come from the owner
  configuration exactly as the zone tools read them (``ATHLETE_FTP_W``,
  ``ATHLETE_CSS_SPEED_MPS``, ``ATHLETE_THRESHOLD_RUN_SPEED_MPS``); a
  missing one is ``status="insufficient_data"`` naming exactly which
  variable is missing — inputs are NEVER guessed (§7.7/§9.3). With all
  inputs present the standard-distance flat-course defaults produce a
  status="ok" result that is NOT persisted (no race row exists to
  reference — documented RP-10 decision).
- **race_id path** (DB-backed): the race row's course profile
  (segments/temperature/altitude) and transitions drive the engine call;
  the prediction IS persisted as a ``PredictionRow`` referencing the race.
  An unknown race id is an error; per-race T1/T2 override transitions
  (which the composite engine API does not accept) are an explicit error,
  never silently ignored.
- **Envelope contract (§7.7)**: every segment and the total carry the FULL
  ``{point, lower, upper, method, assumptions, model_version}`` envelope
  with the engine's own fields VERBATIM — compared here for exact
  equality against a direct engine call with the same inputs (the Monte
  Carlo seed is fixed, so the envelopes are deterministic).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any, Final

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import tools
from app.agent.race_tool import (
    DEFAULT_ALTITUDE_M,
    DEFAULT_BIKE_MASS_KG,
    DEFAULT_CDA_M2,
    DEFAULT_CRR,
    DEFAULT_RIDER_MASS_KG,
    DEFAULT_TEMPERATURE_C,
    RACE_DISTANCE_KEYS,
    REFERENCE_RUN_DISTANCE_M,
    STANDARD_SEGMENTS_M,
)
from app.core.settings import Settings
from app.db.models import PredictionRow, RaceRow
from app.engine.predict import (
    PREDICTION_MODEL_VERSION,
    PredictionEnvelope,
    TriathlonPrediction,
    triathlon_total_prediction,
)

pytestmark = pytest.mark.anyio

ENGINE_VERSION = "test-engine-1.0"
TARGET_DATE = "2026-11-08"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "athlete_ftp_w": 250.0,
        "athlete_css_speed_mps": 0.8333333,
        "athlete_threshold_run_speed_mps": 3.33,
        "athlete_weight_kg": 70.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def race_settings() -> Iterator[Settings]:
    settings = make_settings()
    tools.set_settings_override(settings)
    yield settings
    tools.reset_settings_override()


@pytest.fixture
async def tool_db(
    db_engine: AsyncEngine, race_settings: Settings
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    """Point the tools' session and settings seams at the test fixtures."""
    factory: async_sessionmaker[Any] = async_sessionmaker(
        db_engine, expire_on_commit=False
    )
    tools.set_session_factory_provider(lambda: factory)
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


async def seed(session: AsyncSession, row: Any) -> Any:
    session.add(row)
    await session.commit()
    return row


def make_course(
    *, n_segments: int = 2, temperature_c: float = 20.0, altitude_m: float = 100.0
) -> dict[str, Any]:
    return {
        "segments": [
            {"distance_m": 20000.0, "gradient": 0.0},
            {"distance_m": 5000.0, "gradient": 0.03},
        ][:n_segments],
        "temperature_c": temperature_c,
        "altitude_m": altitude_m,
    }


def engine_reference(
    settings: Settings,
    *,
    bike_segments: list[tuple[float, float]],
    swim_m: float,
    run_m: float,
    race_key: str | None,
    temperature_c: float = DEFAULT_TEMPERATURE_C,
    altitude_m: float = DEFAULT_ALTITUDE_M,
    past_t1_s: list[float] | None = None,
    past_t2_s: list[float] | None = None,
) -> TriathlonPrediction:
    """The engine result for exactly the inputs the tool assembles.

    Mirrors the documented resolution (owner configuration + the module's
    OWNER CHOICE defaults), so the tool's envelopes can be compared for
    VERBATIM equality (the engine's default Monte Carlo seed is fixed).
    """
    assert settings.athlete_ftp_w is not None
    assert settings.athlete_css_speed_mps is not None
    assert settings.athlete_threshold_run_speed_mps is not None
    return triathlon_total_prediction(
        reference_run_distance_m=REFERENCE_RUN_DISTANCE_M,
        reference_run_time_s=REFERENCE_RUN_DISTANCE_M
        / settings.athlete_threshold_run_speed_mps,
        run_distance_m=run_m,
        swim_distance_m=swim_m,
        bike_segments=bike_segments,
        ftp_watts=settings.athlete_ftp_w,
        css_mps=settings.athlete_css_speed_mps,
        cda=DEFAULT_CDA_M2,
        crr=DEFAULT_CRR,
        total_mass_kg=(
            settings.athlete_weight_kg or DEFAULT_RIDER_MASS_KG
        ) + DEFAULT_BIKE_MASS_KG,
        temperature_c=temperature_c,
        altitude_m=altitude_m,
        past_t1_s=past_t1_s or [],
        past_t2_s=past_t2_s or [],
        race_key=race_key,
    )


_SEGMENT_ENGINES: Final = ("swim", "run", "t1", "t2")


def engine_envelope_for(
    prediction: TriathlonPrediction, segment: str
) -> PredictionEnvelope:
    """The engine envelope behind one tool segment name."""
    if segment == "swim":
        return prediction.swim
    if segment == "run":
        return prediction.run
    if segment == "t1":
        return prediction.transitions.t1
    if segment == "t2":
        return prediction.transitions.t2
    if segment.startswith("bike_"):
        return prediction.bike.segments[int(segment.split("_")[1]) - 1]
    if segment == "total":
        return prediction.total
    raise AssertionError(f"unknown segment name {segment!r}")


def assert_envelope_verbatim(out: dict[str, Any], envelope: PredictionEnvelope) -> None:
    """The §7.7 contract: the engine's envelope fields travel VERBATIM."""
    assert out["point"] == envelope.point
    assert out["lower"] == envelope.lower
    assert out["upper"] == envelope.upper
    assert out["method"] == envelope.method
    assert out["assumptions"] == list(envelope.assumptions)
    assert out["model_version"] == envelope.model_version


def assert_envelope_shape(out: dict[str, Any]) -> None:
    """Every envelope is complete and its band encloses its point."""
    for field in ("point", "lower", "upper", "method", "assumptions", "model_version"):
        assert field in out, f"envelope missing {field!r}"
    assert out["lower"] <= out["point"] <= out["upper"]
    assert out["assumptions"], "an envelope without assumptions is a bare number"
    assert out["model_version"] == PREDICTION_MODEL_VERSION


def assert_prediction_verbatim(
    result: dict[str, Any], prediction: TriathlonPrediction
) -> None:
    """Every segment and the total match the engine envelopes VERBATIM."""
    for out in result["segments"]:
        assert_envelope_verbatim(out, engine_envelope_for(prediction, out["segment"]))
    assert_envelope_verbatim(result["total"], prediction.total)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_predict_race_is_registered_in_the_explicit_tool_list(self) -> None:
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in tools.tool_list()
        }
        assert "predict_race" in names


# ---------------------------------------------------------------------------
# Argument validation (errors, never exceptions, never fabricated numbers)
# ---------------------------------------------------------------------------

class TestArgumentValidation:
    async def test_neither_race_id_nor_distance_is_an_error(
        self, race_settings: Settings
    ) -> None:
        result = await tools.predict_race()
        assert result["status"] == "error"
        assert "race_id" in result["detail"]
        assert "distance" in result["detail"]
        assert "segments" not in result
        assert "total" not in result

    async def test_both_race_id_and_distance_is_an_error(
        self, race_settings: Settings
    ) -> None:
        result = await tools.predict_race(race_id=1, distance="olympic")
        assert result["status"] == "error"
        assert "exactly one" in result["detail"].lower()

    async def test_bad_target_date_is_an_error(
        self, race_settings: Settings
    ) -> None:
        result = await tools.predict_race(distance="olympic", target_date="08/11/2026")
        assert result["status"] == "error"
        assert "ISO" in result["detail"]

    async def test_unknown_distance_key_is_an_error_naming_the_engine_vocabulary(
        self, race_settings: Settings
    ) -> None:
        result = await tools.predict_race(distance="ultra")
        assert result["status"] == "error"
        assert "unknown race distance" in result["detail"].lower()
        for key in RACE_DISTANCE_KEYS:
            assert key in result["detail"]
        assert "segments" not in result and "total" not in result


# ---------------------------------------------------------------------------
# Distance path: missing athlete inputs are insufficient_data, never guessed
# ---------------------------------------------------------------------------

class TestDistancePathInsufficientData:
    @pytest.mark.parametrize(
        ("missing_field", "missing_env"),
        [
            ("athlete_ftp_w", "ATHLETE_FTP_W"),
            ("athlete_css_speed_mps", "ATHLETE_CSS_SPEED_MPS"),
            ("athlete_threshold_run_speed_mps", "ATHLETE_THRESHOLD_RUN_SPEED_MPS"),
        ],
    )
    async def test_missing_input_names_exactly_what_is_missing(
        self, missing_field: str, missing_env: str
    ) -> None:
        settings = make_settings(**{missing_field: None})
        tools.set_settings_override(settings)
        try:
            result = await tools.predict_race(distance="olympic")
        finally:
            tools.reset_settings_override()
        assert result["status"] == "insufficient_data"
        assert missing_env in result["detail"]
        assert result["race_key"] == "olympic"
        # Never a fabricated prediction and never persisted.
        assert "segments" not in result and "total" not in result
        assert result["coverage"]["persisted"] is False
        assert result["coverage"].get("prediction_id") is None


# ---------------------------------------------------------------------------
# Distance path: happy path (settings only, no DB, NOT persisted)
# ---------------------------------------------------------------------------

class TestDistancePathHappyPath:
    async def test_ok_result_shape_and_coverage(self, race_settings: Settings) -> None:
        result = await tools.predict_race(
            distance="olympic", target_date=TARGET_DATE
        )
        assert result["status"] == "ok"
        assert result["race_key"] == "olympic"
        assert result["target_date"] == TARGET_DATE
        swim_m, bike_m, run_m = STANDARD_SEGMENTS_M["olympic"]
        assert swim_m == 1500.0 and bike_m == 40000.0 and run_m == 10000.0
        assert [s["segment"] for s in result["segments"]] == [
            "swim", "bike_1", "run", "t1", "t2",
        ]
        for segment in result["segments"]:
            assert_envelope_shape(segment)
        assert_envelope_shape(result["total"])
        coverage = result["coverage"]
        assert coverage["path"] == "distance"
        assert coverage["course_segments"] == 0  # standard flat course assumed
        assert coverage["persisted"] is False
        assert coverage.get("prediction_id") is None
        assert coverage["transitions"] == "defaults for 'olympic'"
        assert coverage["athlete_inputs"]["ftp_watts"] == (
            "owner configuration (ATHLETE_FTP_W)"
        )
        assert coverage["athlete_inputs"]["css_mps"] == (
            "owner configuration (ATHLETE_CSS_SPEED_MPS)"
        )
        assert coverage["athlete_inputs"]["threshold_run_speed_mps"] == (
            "owner configuration (ATHLETE_THRESHOLD_RUN_SPEED_MPS)"
        )
        assert "default" in coverage["athlete_inputs"]["cda_m2"].lower()
        assert "default" in coverage["athlete_inputs"]["crr"].lower()
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"]

    async def test_envelopes_travel_verbatim_from_the_engine(
        self, race_settings: Settings
    ) -> None:
        result = await tools.predict_race(distance="olympic")
        prediction = engine_reference(
            race_settings,
            bike_segments=[(40000.0, 0.0)],
            swim_m=1500.0,
            run_m=10000.0,
            race_key="olympic",
        )
        assert_prediction_verbatim(result, prediction)

    async def test_bounds_are_stable_across_calls(
        self, race_settings: Settings
    ) -> None:
        first = await tools.predict_race(distance="half")
        second = await tools.predict_race(distance="half")
        assert first["total"]["lower"] == second["total"]["lower"]
        assert first["total"]["upper"] == second["total"]["upper"]

    async def test_coverage_names_the_run_anchor_effort_and_its_origin(
        self, race_settings: Settings
    ) -> None:
        """The tool must state WHICH anchor effort the run prediction used
        and WHERE it came from: the documented derivation (5000 m at the
        owner-configured threshold run speed), never an invented effort."""
        result = await tools.predict_race(distance="olympic")
        assert result["status"] == "ok"
        anchor = result["coverage"]["run_anchor"]
        assert "5000 m" in anchor
        assert "3.33" in anchor  # the configured threshold speed
        assert "1501.5" in anchor  # 5000 / 3.33 seconds
        assert "ATHLETE_THRESHOLD_RUN_SPEED_MPS" in anchor

    async def test_unconfigured_weight_uses_the_documented_default(
        self, race_settings: Settings
    ) -> None:
        settings = make_settings(athlete_weight_kg=None)
        tools.set_settings_override(settings)
        try:
            result = await tools.predict_race(distance="sprint")
        finally:
            tools.reset_settings_override()
        assert result["status"] == "ok"
        assert "default" in result["coverage"]["athlete_inputs"]["rider_mass_kg"].lower()
        prediction = engine_reference(
            settings,
            bike_segments=[(20000.0, 0.0)],
            swim_m=750.0,
            run_m=5000.0,
            race_key="sprint",
        )
        assert_envelope_verbatim(result["total"], prediction.total)

    async def test_every_standard_distance_key_predicts(
        self, race_settings: Settings
    ) -> None:
        for key in RACE_DISTANCE_KEYS:
            result = await tools.predict_race(distance=key)
            assert result["status"] == "ok", key
            assert result["race_key"] == key


# ---------------------------------------------------------------------------
# race_id path (DB-backed; skips cleanly without Postgres)
# ---------------------------------------------------------------------------

class TestRaceIdPath:
    async def test_course_and_transitions_drive_the_persisted_prediction(
        self, tool_db: async_sessionmaker[Any], race_settings: Settings
    ) -> None:
        async with tool_db() as session:
            race = await seed(
                session,
                RaceRow(
                    race_key="olympic",
                    name="Hometown Tri",
                    course=make_course(temperature_c=20.0, altitude_m=100.0),
                    transitions={"past_t1_s": [150.0, 170.0], "past_t2_s": [80.0]},
                    goal="sub 2:30",
                ),
            )

        result = await tools.predict_race(race_id=race.id, target_date=TARGET_DATE)

        assert result["status"] == "ok"
        assert result["race_key"] == "olympic"
        assert result["target_date"] == TARGET_DATE
        coverage = result["coverage"]
        assert coverage["path"] == "race_id"
        assert coverage["course_segments"] == 2
        assert coverage["temperature_c"] == 20.0
        assert coverage["altitude_m"] == 100.0
        assert "past data" in coverage["transitions"]
        assert coverage["persisted"] is True
        assert isinstance(coverage["prediction_id"], int)
        assert [s["segment"] for s in result["segments"]] == [
            "swim", "bike_1", "bike_2", "run", "t1", "t2",
        ]

        prediction = engine_reference(
            race_settings,
            bike_segments=[(20000.0, 0.0), (5000.0, 0.03)],
            swim_m=1500.0,
            run_m=10000.0,
            race_key="olympic",
            temperature_c=20.0,
            altitude_m=100.0,
            past_t1_s=[150.0, 170.0],
            past_t2_s=[80.0],
        )
        assert_prediction_verbatim(result, prediction)

        async with tool_db() as session:
            rows = (await session.execute(select(PredictionRow))).scalars().all()
        assert len(rows) == 1
        stored = rows[0]
        assert stored.race_id == race.id
        assert stored.race_key == "olympic"
        assert stored.target_date is not None and stored.target_date.isoformat() == TARGET_DATE
        assert stored.engine_version == ENGINE_VERSION
        assert stored.created_at is not None
        assert [s["segment"] for s in stored.segments] == [
            "swim", "bike_1", "bike_2", "run", "t1", "t2",
        ]
        for segment in stored.segments:
            assert_envelope_shape(segment)
        assert_envelope_shape(stored.total)
        assert stored.total["method"] == result["total"]["method"]

    async def test_coverage_reports_the_run_anchor_on_the_race_path(
        self, tool_db: async_sessionmaker[Any], race_settings: Settings
    ) -> None:
        """The race_id path derives the SAME reference effort; its coverage
        names it and its origin too."""
        async with tool_db() as session:
            race = await seed(
                session, RaceRow(race_key="olympic", name="Anchor Race")
            )
        result = await tools.predict_race(race_id=race.id)
        assert result["status"] == "ok"
        anchor = result["coverage"]["run_anchor"]
        assert "5000 m" in anchor
        assert "ATHLETE_THRESHOLD_RUN_SPEED_MPS" in anchor

    async def test_unknown_race_id_is_an_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.predict_race(race_id=999)
        assert result["status"] == "error"
        assert "no race with id 999" in result["detail"].lower()
        assert "segments" not in result and "total" not in result
        async with tool_db() as session:
            rows = (await session.execute(select(PredictionRow))).scalars().all()
        assert rows == []  # nothing persisted on the error path

    async def test_custom_race_key_uses_the_course_swim_run_split(
        self, tool_db: async_sessionmaker[Any], race_settings: Settings
    ) -> None:
        course = make_course(n_segments=1)
        course["swim_distance_m"] = 900.0
        course["run_distance_m"] = 8000.0
        async with tool_db() as session:
            race = await seed(session, RaceRow(race_key="my_a_race", course=course))
        result = await tools.predict_race(race_id=race.id)
        assert result["status"] == "ok"
        assert result["race_key"] == "my_a_race"
        assert result["coverage"]["transitions"] == (
            "generic defaults (custom race key)"
        )
        # The generic default T1 (300 s) reaches the envelope assumptions
        # VERBATIM (the engine's transition defaults, not a local number).
        t1 = next(s for s in result["segments"] if s["segment"] == "t1")
        assert "300" in " ".join(t1["assumptions"])
        prediction = engine_reference(
            race_settings,
            bike_segments=[(20000.0, 0.0)],
            swim_m=900.0,
            run_m=8000.0,
            race_key=None,
            temperature_c=20.0,
            altitude_m=100.0,
        )
        assert_prediction_verbatim(result, prediction)

    async def test_custom_race_key_without_a_swim_run_split_is_an_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            race = await seed(
                session, RaceRow(race_key="my_a_race", course=make_course(n_segments=1))
            )
        result = await tools.predict_race(race_id=race.id)
        assert result["status"] == "error"
        assert "swim_distance_m" in result["detail"]
        assert "run_distance_m" in result["detail"]
        assert "segments" not in result and "total" not in result

    async def test_race_row_without_course_uses_the_standard_flat_course(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            race = await seed(session, RaceRow(race_key="sprint"))
        result = await tools.predict_race(race_id=race.id)
        assert result["status"] == "ok"
        assert result["coverage"]["course_segments"] == 0
        assert result["coverage"]["temperature_c"] == DEFAULT_TEMPERATURE_C

    async def test_missing_athlete_inputs_are_insufficient_data(
        self, db_engine: AsyncEngine
    ) -> None:
        factory: async_sessionmaker[Any] = async_sessionmaker(
            db_engine, expire_on_commit=False
        )
        settings = make_settings(athlete_ftp_w=None)
        tools.set_session_factory_provider(lambda: factory)
        tools.set_settings_override(settings)
        try:
            async with factory() as session:
                race = await seed(session, RaceRow(race_key="half"))
            result = await tools.predict_race(race_id=race.id)
        finally:
            tools.reset_settings_override()
            tools.reset_session_factory_provider()
        assert result["status"] == "insufficient_data"
        assert "ATHLETE_FTP_W" in result["detail"]
        assert result["race_key"] == "half"
        assert result["coverage"]["persisted"] is False

    async def test_transition_override_shape_is_an_explicit_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            race = await seed(
                session,
                RaceRow(
                    race_key="sprint",
                    course=make_course(n_segments=1),
                    transitions={"t1_override_s": 120.0},
                ),
            )
        result = await tools.predict_race(race_id=race.id)
        assert result["status"] == "error"
        assert "override" in result["detail"].lower()
        assert "segments" not in result and "total" not in result
