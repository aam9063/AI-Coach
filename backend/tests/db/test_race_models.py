"""RP-10 DB tests: the ``race`` and ``prediction`` tables (brief §6/§7.7).

Proves the two Feature 8 tables round-trip through Postgres following the
existing model conventions (JSONB payloads, ``engine_version`` provenance,
``_utcnow`` ``created_at`` defaults):

- ``race``: one row per target race — ``race_key`` (the engine's race
  distance vocabulary or a custom label), nullable name/course/goal; the
  ``course`` JSONB carries the elevation/gradient profile segments plus the
  expected temperature and altitude; ``transitions`` carries past T1/T2
  observations (or per-race overrides).
- ``prediction``: one row per persisted race prediction — nullable
  ``race_id`` FK (distance-only predictions have no race row to
  reference), NOT NULL ``race_key``, nullable ``target_date``, the
  per-segment envelopes and the composite total as JSONB (each envelope
  {point, lower, upper, method, assumptions, model_version} verbatim),
  ``engine_version`` and ``created_at``.

The FK is REAL: a prediction referencing a non-existent race is rejected
by Postgres, never silently stored.

Requires the compose Postgres; skips cleanly without it (see conftest).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PredictionRow, RaceRow

pytestmark = pytest.mark.anyio

ENGINE_VERSION = "test-0.0.0"
TARGET_DATE = dt.date(2026, 11, 8)

COURSE = {
    "segments": [
        {"distance_m": 20000.0, "gradient": 0.0},
        {"distance_m": 5000.0, "gradient": 0.03},
    ],
    "temperature_c": 22.0,
    "altitude_m": 150.0,
}
TRANSITIONS = {"past_t1_s": [150.0, 170.0], "past_t2_s": [80.0, 95.0]}
SEGMENTS_JSON = [
    {
        "segment": "swim",
        "point": 1000.0,
        "lower": 950.0,
        "upper": 1100.0,
        "method": "css_fraction_open_water_monte_carlo",
        "assumptions": ["speed = CSS * 1.00 / 1.03", "band: CSS +/- 2*0.02 relative"],
        "model_version": "predict-1.0.0",
    },
]
TOTAL_JSON = {
    "point": 9000.0,
    "lower": 8500.0,
    "upper": 9600.0,
    "method": "monte_carlo_composite",
    "assumptions": ["total = swim + bike + run-off-the-bike + T1 + T2"],
    "model_version": "predict-1.0.0",
}


async def test_race_row_round_trip(db_session: AsyncSession) -> None:
    row = RaceRow(
        race_key="olympic",
        name="Hometown Tri",
        course=COURSE,
        transitions=TRANSITIONS,
        goal="sub 2:30",
    )
    db_session.add(row)
    await db_session.commit()

    fresh = (
        (await db_session.execute(select(RaceRow).where(RaceRow.race_key == "olympic")))
        .scalars()
        .one()
    )
    assert fresh.id is not None
    assert fresh.name == "Hometown Tri"
    assert fresh.course == COURSE
    assert fresh.transitions == TRANSITIONS
    assert fresh.goal == "sub 2:30"
    assert fresh.created_at is not None  # the _utcnow default convention


async def test_race_row_minimal_only_race_key(db_session: AsyncSession) -> None:
    row = RaceRow(race_key="sprint")
    db_session.add(row)
    await db_session.commit()

    fresh = (
        (await db_session.execute(select(RaceRow).where(RaceRow.race_key == "sprint")))
        .scalars()
        .one()
    )
    assert fresh.name is None
    assert fresh.course is None
    assert fresh.transitions is None
    assert fresh.goal is None
    assert fresh.created_at is not None


async def test_prediction_row_round_trip_with_race_reference(
    db_session: AsyncSession,
) -> None:
    race = RaceRow(race_key="half", name="70.3")
    db_session.add(race)
    await db_session.flush()
    row = PredictionRow(
        race_id=race.id,
        race_key="half",
        target_date=TARGET_DATE,
        segments=SEGMENTS_JSON,
        total=TOTAL_JSON,
        engine_version=ENGINE_VERSION,
    )
    db_session.add(row)
    await db_session.commit()

    fresh = (
        (await db_session.execute(select(PredictionRow).where(PredictionRow.id == row.id)))
        .scalars()
        .one()
    )
    assert fresh.race_id == race.id
    assert fresh.race_key == "half"
    assert fresh.target_date == TARGET_DATE
    # The envelope JSONB round-trips verbatim, assumptions lists included.
    assert fresh.segments == SEGMENTS_JSON
    assert fresh.total == TOTAL_JSON
    assert fresh.engine_version == ENGINE_VERSION
    assert fresh.created_at is not None


async def test_distance_only_prediction_has_no_race_reference(
    db_session: AsyncSession,
) -> None:
    row = PredictionRow(
        race_id=None,
        race_key="olympic",
        target_date=None,
        segments=SEGMENTS_JSON,
        total=TOTAL_JSON,
        engine_version=ENGINE_VERSION,
    )
    db_session.add(row)
    await db_session.commit()

    fresh = (
        (await db_session.execute(select(PredictionRow).where(PredictionRow.id == row.id)))
        .scalars()
        .one()
    )
    assert fresh.race_id is None
    assert fresh.target_date is None


async def test_prediction_race_id_is_a_real_foreign_key(
    db_session: AsyncSession,
) -> None:
    row = PredictionRow(
        race_id=987654,
        race_key="olympic",
        segments=SEGMENTS_JSON,
        total=TOTAL_JSON,
        engine_version=ENGINE_VERSION,
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.commit()
