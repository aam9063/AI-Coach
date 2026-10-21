"""Fixed DB fixtures for the golden set (EVAL-3, PROJECT_BRIEF section 13).

Every golden case names ONE builder from :data:`FIXTURE_REGISTRY` through
its ``db_fixture`` field; the runner seeds it before the turn. The builders
make the evals run deterministically WITHOUT live ingestion (section 13):

- **no random**: every value is a literal constant or computed by the pure
  engine from fixed constants;
- **no wall clock in the seeded data**: all dates are fixed (the single
  exception is ``intensity_weeks``, which must seed the ISO weeks the tool
  itself derives from "now" — documented on that builder);
- **engine-derived numbers**: the ``daily_load`` rows are computed by
  :func:`app.engine.pmc.compute_pmc` from a FIXED TSS series (the persisted
  engine output of record), never invented; the readiness and durability
  rows copy engine-shaped anchors from the verified test suite
  (``tests/agent/test_agent_tools.py``) — the same values the golden
  expectations cite as provenance.

Builders only INSERT rows; the runner owns a fresh-schema engine per case
(the ``tests/conftest.py`` ``db_engine`` convention on the dedicated
``*_test`` database).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from datetime import UTC
from typing import Any, Final

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityRow,
    AthleteProfileRow,
    Base,
    DailyLoadRow,
    ReadinessSnapshotRow,
    SessionDurabilityRow,
    WeeklyIntensityRow,
    WellnessRow,
)
from app.db.repository import upsert_evidence_chunk, upsert_evidence_source
from app.engine.pmc import compute_pmc, compute_pmc_per_sport
from app.evidence.embeddings import FakeEmbedding

ENGINE_VERSION: Final[str] = "eval-engine-1.0"
COMPUTED_AT: Final[dt.datetime] = dt.datetime(2026, 9, 7, 6, 0, tzinfo=UTC)

# --- The fixed load series (the engine computes the CTL/ATL/TSB rows) ------

LOAD_START: Final[dt.date] = dt.date(2026, 6, 1)  # Monday
LOAD_END: Final[dt.date] = dt.date(2026, 9, 7)  # Monday, 99 days
WEEK_TSS_PATTERN: Final[tuple[float, ...]] = (60.0, 0.0, 45.0, 70.0, 0.0, 90.0, 30.0)
RIDE_SHARE: Final[float] = 0.6
"""Per-sport split of every day's TSS (ride 60% / run 40%) so per-sport
rows exist alongside the combined row, exactly as the engine's
per-sport PMC output does."""


def _fixed_tss_series() -> dict[dt.date, float]:
    """The fixture's fixed daily TSS series (contiguous, rest days as 0.0)."""
    series: dict[dt.date, float] = {}
    day = LOAD_START
    while day <= LOAD_END:
        series[day] = WEEK_TSS_PATTERN[day.weekday()]
        day += dt.timedelta(days=1)
    return series


def _daily_load_row(
    day: dt.date, sport: str, tss: float, ctl: float, atl: float, tsb: float
) -> DailyLoadRow:
    return DailyLoadRow(
        athlete_id=1,
        date=day,
        sport=sport,
        tss=tss,
        ctl=ctl,
        atl=atl,
        tsb=tsb,
        engine_version=ENGINE_VERSION,
        computed_at=COMPUTED_AT,
    )


async def seed_load_window(session: AsyncSession) -> None:
    """Engine-computed daily_load rows for 2026-06-01..2026-09-07.

    The rows are the pure engine's output (:func:`compute_pmc` /
    :func:`compute_pmc_per_sport`) over the FIXED TSS series above — the
    same computation the golden expectations cite as provenance — so a
    fixture value is an engine value by construction, never invented.
    """
    tss = _fixed_tss_series()
    combined = compute_pmc(tss)
    per_sport = compute_pmc_per_sport(
        {
            "ride": {d: round(t * RIDE_SHARE, 1) for d, t in tss.items()},
            "run": {d: round(t * (1.0 - RIDE_SHARE), 1) for d, t in tss.items()},
        }
    )
    for day_series, sport in (
        (combined, "combined"),
        (per_sport.per_sport["ride"], "ride"),
        (per_sport.per_sport["run"], "run"),
    ):
        for pmc_day in day_series.days:
            session.add(
                _daily_load_row(
                    pmc_day.date,
                    sport,
                    pmc_day.tss,
                    pmc_day.ctl,
                    pmc_day.atl,
                    pmc_day.tsb,
                )
            )
    await session.commit()


# --- Zones: the profile thresholds the pure engine builds tables from ------

async def seed_zones_profile(session: AsyncSession) -> None:
    """The athlete_profile row of record: FTP 210 W (manual) and CSS
    0.8333333 m/s (css_from_time_trials). LTHR travels in the runner's
    settings (the profile does not store it) — the same resolution
    ``get_zones`` performs."""
    session.add(
        AthleteProfileRow(
            athlete_id=1,
            ftp_watts=210.0,
            ftp_source="manual",
            css_mps=0.8333333,
            css_source="css_from_time_trials",
            engine_version=ENGINE_VERSION,
            updated_at=COMPUTED_AT,
        )
    )
    await session.commit()


# --- Readiness: engine-shaped snapshot rows (RID-10 anchors) ---------------

_HRV_OK_SIGNAL: Final[dict[str, object]] = {
    "key": "hrv_ln_rmssd",
    "status": "ok",
    "observed": 4.17,
    "baseline": 4.1,
    "baseline_mean": 4.1,
    "baseline_sd": 0.2,
    "band_half_width_sd": 0.5,
    "direction": "up",
    "confidence": "moderate",
}
_HRV_INSUFFICIENT_SIGNAL: Final[dict[str, object]] = {
    "key": "hrv_ln_rmssd",
    "status": "insufficient_data",
    "observed": None,
    "baseline": None,
    "direction": None,
    "confidence": None,
}
_RHR_INSUFFICIENT_SIGNAL: Final[dict[str, object]] = {
    "key": "resting_hr",
    "status": "insufficient_data",
    "observed": None,
    "baseline": None,
    "direction": None,
    "confidence": None,
}
_RHR_OK_SIGNAL: Final[dict[str, object]] = {
    "key": "resting_hr",
    "status": "ok",
    "observed": 52.0,
    "baseline": 50.0,
    "direction": "up",
    "confidence": "moderate",
}


def _readiness_snapshot(
    day: dt.date, *, signals: list[dict[str, object]], agreement: int, suggest: bool
) -> ReadinessSnapshotRow:
    return ReadinessSnapshotRow(
        athlete_id=1,
        date=day,
        signals=signals,
        adverse_signal_keys=["tsb_very_negative"],
        agreement_count=agreement,
        suggest_reduce_intensity=suggest,
        suggestion=("Reduce la intensidad de hoy." if suggest else None),
        reasons=["TSB is below the very-negative threshold."],
        tsb=-12.0,
        tsb_very_negative_below=-10.0,
        subjective_fatigue_reported=None,
        acwr=None,
        engine_version=ENGINE_VERSION,
        computed_at=COMPUTED_AT,
    )


async def seed_readiness_ok(session: AsyncSession) -> None:
    """The 2026-09-07 snapshot anchored on
    ``tests/agent/test_agent_tools.py::make_readiness_snapshot``: HRV ok
    (observed 4.17 vs baseline 4.1), resting HR insufficient, one adverse
    signal, no engine warning."""
    session.add(
        _readiness_snapshot(
            dt.date(2026, 9, 7),
            signals=[dict(_HRV_OK_SIGNAL), dict(_RHR_INSUFFICIENT_SIGNAL)],
            agreement=1,
            suggest=False,
        )
    )
    await session.commit()


async def seed_readiness_warning(session: AsyncSession) -> None:
    """The 2026-09-07 snapshot with the engine's two-signal warning rule
    fired: HRV below baseline AND resting HR above baseline ->
    agreement_count 2, suggest_reduce_intensity True."""
    session.add(
        _readiness_snapshot(
            dt.date(2026, 9, 7),
            signals=[dict(_HRV_OK_SIGNAL), dict(_RHR_OK_SIGNAL)],
            agreement=2,
            suggest=True,
        )
    )
    await session.commit()


async def seed_hrv_wellness(session: AsyncSession) -> None:
    """Wellness ln(rMSSD) rows AND the readiness snapshots whose baseline
    band the ``hrv`` chart needs — one day of each across
    2026-09-01..2026-09-07 (the chart draws a day only when BOTH exist)."""
    day = dt.date(2026, 9, 1)
    index = 0
    while day <= dt.date(2026, 9, 7):
        session.add(
            WellnessRow(
                athlete_id=1,
                date=day,
                hrv=round(60.0 + index, 1),
                ln_hrv=round(4.1 + 0.02 * index, 3),
                resting_hr=50,
                sleep_minutes=420,
            )
        )
        session.add(
            _readiness_snapshot(
                day,
                signals=[dict(_HRV_OK_SIGNAL), dict(_RHR_INSUFFICIENT_SIGNAL)],
                agreement=1,
                suggest=False,
            )
        )
        day += dt.timedelta(days=1)
        index += 1
    await session.commit()


# --- Activities and durability (anchors from test_agent_tools) --------------


def _activity_501() -> ActivityRow:
    return ActivityRow(
        id=501,
        source="intervals",
        source_id="i501",
        type="Ride",
        name="Salida larga",
        start_time=dt.datetime(2026, 9, 5, 8, 0, tzinfo=UTC),
        duration_s=5400,
        distance_m=40000.0,
    )


async def seed_activity_durability(session: AsyncSession) -> None:
    """Activity 501 plus its engine durability row (anchor values:
    ef 0.98/0.95, decoupling 3.1%, inside the 5% reference band)."""
    session.add(_activity_501())
    session.add(
        SessionDurabilityRow(
            activity_id=501,
            sport="bike",
            status="ok",
            ef_first_half=0.98,
            ef_second_half=0.95,
            decoupling=0.031,
            decoupling_pct=3.1,
            within_reference_band=True,
            reference_band=0.05,
            intensity_first_half=210.0,
            intensity_second_half=203.0,
            intensity_drift=0.033,
            max_intensity_drift=0.15,
            n_samples=7200,
            n_first_half=3600,
            n_second_half=3600,
            detail="",
            engine_version=ENGINE_VERSION,
            computed_at=COMPUTED_AT,
        )
    )
    await session.commit()


async def seed_activity_two(session: AsyncSession) -> None:
    """Two activities; the most recent (502, a run) has NO durability row,
    so ``get_activity_analysis`` with "last" reports the session summary
    and an explicit insufficient-data durability entry."""
    session.add(_activity_501())
    session.add(
        ActivityRow(
            id=502,
            source="intervals",
            source_id="i502",
            type="Run",
            name="Carrera continua",
            start_time=dt.datetime(2026, 9, 6, 9, 0, tzinfo=UTC),
            duration_s=3600,
            distance_m=10000.0,
        )
    )
    await session.commit()


# --- Weekly intensity (anchor percentages 75/15/10) -------------------------

INTENSITY_SECONDS: Final[tuple[float, float, float]] = (27000.0, 5400.0, 3600.0)
INTENSITY_PERCENTAGES: Final[list[float]] = [75.0, 15.0, 10.0]


async def seed_intensity_weeks(session: AsyncSession) -> None:
    """Two weekly_intensity rows (the current ISO week and the previous
    one) with the 75/15/10 anchor percentages.

    The ONE deliberate wall-clock use in the fixtures, documented:
    ``get_intensity_distribution`` derives its window from the CURRENT ISO
    week, so the fixture must seed that week (and the previous one) at
    seed time. The expectations do not depend on WHICH week it is — the
    percentages are the anchor — so the case stays deterministic within a
    run (the seed and the tool call are microseconds apart).
    """
    today = dt.date.today()
    year, week, _ = today.isocalendar()
    weeks: list[tuple[int, int]] = []
    y, w = year, week
    for _ in range(2):
        weeks.append((y, w))
        w -= 1
        if w == 0:
            y -= 1
            w = dt.date(y, 12, 28).isocalendar()[1]
    for iso_year, iso_week in weeks:
        session.add(
            WeeklyIntensityRow(
                athlete_id=1,
                iso_year=iso_year,
                iso_week=iso_week,
                week_start=dt.date.fromisocalendar(iso_year, iso_week, 1),
                sport="bike",
                status="data",
                z1_seconds=INTENSITY_SECONDS[0],
                z2_seconds=INTENSITY_SECONDS[1],
                z3_seconds=INTENSITY_SECONDS[2],
                total_seconds=sum(INTENSITY_SECONDS),
                percentages=list(INTENSITY_PERCENTAGES),
                engine_version=ENGINE_VERSION,
                computed_at=COMPUTED_AT,
            )
        )
    await session.commit()


# --- Evidence library (paper + guide, deterministic fake embeddings) --------

PAPER_CITATION: Final[str] = "(Stephen Seiler, 2010)"
GUIDE_CITATION: Final[str] = "(Rocío Martínez, 2024, training guide)"

_PAPER_CHUNK_ES: Final[str] = (
    "La distribución polarizada de la intensidad del entrenamiento de "
    "resistencia dedica aproximadamente el 80 por ciento del volumen a "
    "intensidad baja, por debajo del primer umbral ventilatorio, y el resto "
    "repartido entre intensidad moderada y alta. Los estudios de ciclistas y "
    "corredores de resistencia muestran que esta distribución de la "
    "intensidad del entrenamiento sostiene mejoras prolongadas del "
    "rendimiento."
)
_PAPER_CHUNK_EN: Final[str] = (
    "Polarized training intensity distribution: elite endurance athletes "
    "spend about 80 percent of their training volume at low intensity, below "
    "the first ventilatory threshold, with the rest split between moderate "
    "and high intensity. Studies of cyclists and runners show this polarized "
    "intensity distribution sustains long-term improvements in endurance "
    "performance."
)
_GUIDE_CHUNK_ES: Final[str] = (
    "Para subir el FTP conviene acumular entrenamiento en sweet spot: "
    "bloques de 3x15 minutos al 88-94 por ciento del FTP. La guía recomienda "
    "dos sesiones de sweet spot por semana dentro de una distribución "
    "polarizada de la intensidad."
)


async def seed_evidence_library(session: AsyncSession) -> None:
    """The curated evidence store: one paper (two chunks, ES + EN) and one
    training guide (one chunk), embedded with the deterministic
    :class:`FakeEmbedding` so retrieval runs offline and repeatably."""
    embedding = FakeEmbedding()
    paper = await upsert_evidence_source(
        session,
        kind="paper",
        slug="seiler-polarized-2010",
        title="Polarized training intensity distribution",
        authors=["Stephen Seiler"],
        year=2010,
        journal="IJSPP",
        doi="10.1000/seiler-2010",
        citation=PAPER_CITATION,
    )
    guide = await upsert_evidence_source(
        session,
        kind="guide",
        slug="pedalea-con-ciencia",
        title="Pedalea con Ciencia",
        authors=["Rocío Martínez"],
        year=2024,
        journal=None,
        doi=None,
        citation=GUIDE_CITATION,
    )
    for index, text in enumerate((_PAPER_CHUNK_ES, _PAPER_CHUNK_EN)):
        await upsert_evidence_chunk(
            session,
            source_id=paper.id,
            chunk_index=index,
            text=text,
            embedding=embedding.embed([text])[0],
            citation=PAPER_CITATION,
        )
    await upsert_evidence_chunk(
        session,
        source_id=guide.id,
        chunk_index=0,
        text=_GUIDE_CHUNK_ES,
        embedding=embedding.embed([_GUIDE_CHUNK_ES])[0],
        citation=GUIDE_CITATION,
    )
    await session.commit()


# --- Workout planner composites (workout-planner T5 golden cases) ----------
#
# ``plan_workout`` reads THREE persisted inputs per call (the day's readiness
# snapshot, the day's combined daily_load row and the last-7-days activity
# mix) and resolves zone thresholds itself, so each planner case needs a
# composite builder. They REUSE the existing seed functions wherever the
# existing fixture already holds the right rows and add only the minimum
# extra data; every number is engine-computed, never invented.

_PLANNER_PLAN_DAY: Final[dt.date] = dt.date(2026, 9, 7)
"""The planner cases' plan day: the fixture anchor date. Every planner case
scripts ``day=2026-09-07`` explicitly because ``plan_workout`` with
``day=None`` resolves to the REAL current date, which would be
non-deterministic against the fixed fixtures."""

_OVERREACH_SPIKES: Final[dict[dt.date, float]] = {
    dt.date(2026, 9, 6): 150.0,
    dt.date(2026, 9, 7): 150.0,
}
"""Extra TSS on top of the fixed weekly pattern that pushes the engine's TSB
for the plan day into the overreached band (decision-table row 2):
:func:`compute_pmc` over the spiked series gives TSB -21.3093... on
2026-09-07 — the value the planner-rest golden case cites as provenance."""


async def seed_planner_recovery(session: AsyncSession) -> None:
    """Decision-table row 1 (readiness suggests reduce intensity):
    ``load_window`` plus the warning readiness snapshot
    (``suggest_reduce_intensity=True``) on the plan day. Nothing else: the
    case names no sport and seeds no activity mix, so the row-4 refinement
    cannot fire."""
    await seed_load_window(session)
    await seed_readiness_warning(session)


def _overreached_series() -> dict[dt.date, float]:
    """The fixture's fixed TSS series plus :data:`_OVERREACH_SPIKES`."""
    series = _fixed_tss_series()
    for day, extra in _OVERREACH_SPIKES.items():
        series[day] = series[day] + extra
    return series


async def seed_planner_rest(session: AsyncSession) -> None:
    """Decision-table row 2 (TSB strictly below the very-negative threshold,
    the band reused from readiness): the ok readiness snapshot plus
    engine-computed combined daily_load rows over the spiked series. Only the
    combined rows are seeded — ``plan_workout`` reads exactly that row (and
    the readiness snapshot), so per-sport rows would be unused fixture data.
    The base ``load_window`` TSB (-4.87) is NOT in the overreached band, which
    is why this fixture exists at all."""
    await seed_readiness_ok(session)
    for pmc_day in compute_pmc(_overreached_series()).days:
        session.add(
            _daily_load_row(
                pmc_day.date,
                "combined",
                pmc_day.tss,
                pmc_day.ctl,
                pmc_day.atl,
                pmc_day.tsb,
            )
        )
    await session.commit()


async def seed_planner_quality(session: AsyncSession) -> None:
    """Decision-table row 3 (TSB neutral/positive AND readiness ok):
    ``load_window`` plus the ok readiness snapshot. The case names no sport
    and seeds no activity mix, so the session carries the Z3 KEY with
    honestly unnumbered bounds (the tool attaches no numeric zone values
    without a zone model for the session's sport) — an unnumbered key is
    honest, an invented number is not."""
    await seed_load_window(session)
    await seed_readiness_ok(session)


async def seed_planner_sport_mix(session: AsyncSession) -> None:
    """Decision-table row 4 (sport imbalance): ``load_window``, the ok
    readiness snapshot, and a 7-day mix of 5 Ride + 1 Run inside the plan
    day's window (2026-09-01..09-06, all before 2026-09-08 exclusive): ride
    share 5/6 = 0.83 > 0.70, so with NO sport named in the request the tool
    must decide the least-share sport ("run")."""
    await seed_load_window(session)
    await seed_readiness_ok(session)
    for index in range(5):
        session.add(
            ActivityRow(
                id=601 + index,
                source="intervals",
                source_id=f"i60{index}",
                type="Ride",
                name=f"Rodillo {index + 1}",
                start_time=dt.datetime(2026, 9, 1 + index, 8, 0, tzinfo=UTC),
                duration_s=3600,
                distance_m=30000.0,
            )
        )
    session.add(
        ActivityRow(
            id=606,
            source="intervals",
            source_id="i606",
            type="Run",
            name="Carrera continua",
            start_time=dt.datetime(2026, 9, 6, 9, 0, tzinfo=UTC),
            duration_s=3600,
            distance_m=10000.0,
        )
    )
    await session.commit()


# --- The registry -----------------------------------------------------------

Builder = Callable[[AsyncSession], Awaitable[None]]

FIXTURE_REGISTRY: Final[dict[str, Builder]] = {
    "none": lambda session: _seed_nothing(),
    "load_window": seed_load_window,
    "zones_profile": seed_zones_profile,
    "readiness_ok": seed_readiness_ok,
    "readiness_warning": seed_readiness_warning,
    "hrv_wellness": seed_hrv_wellness,
    "activity_durability": seed_activity_durability,
    "activity_two": seed_activity_two,
    "intensity_weeks": seed_intensity_weeks,
    "evidence_library": seed_evidence_library,
    "planner_recovery": seed_planner_recovery,
    "planner_rest": seed_planner_rest,
    "planner_quality": seed_planner_quality,
    "planner_sport_mix": seed_planner_sport_mix,
}


async def _seed_nothing() -> None:
    """The explicit empty fixture: cases that need no seeded data (or whose
    point IS the empty database: missing-data cases)."""


def fixture_builder(name: str) -> Builder:
    """Resolve a case's ``db_fixture`` name against the registry.

    Raises:
        KeyError: unknown fixture name (a curation error, named precisely).
    """
    try:
        return FIXTURE_REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown db_fixture {name!r}: golden cases must name a builder "
            f"from FIXTURE_REGISTRY {sorted(FIXTURE_REGISTRY)} or 'none'"
        ) from None


async def clear_all_rows(session_factory: Any) -> None:
    """Delete every row from every table (reverse dependency order).

    The runner calls this before each case so cases stay independent even
    when they share one engine: a case's fixtures never see another case's
    rows. Test-side only, on the dedicated test database.
    """
    async with session_factory() as session:
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(delete(table))
        await session.commit()


async def apply_fixture(name: str, session: AsyncSession) -> None:
    """Seed the named fixture into a fresh-schema session."""
    await fixture_builder(name)(session)
