"""T3 of ODD feature workout-planner (spec S4): the model-facing tool.

``plan_workout`` is a THIN wrapper like every other engine-backed tool: it
reads the persisted inputs through the ``tool_session()`` seam (readiness
snapshot, the day's combined ``daily_load`` row, the configured thresholds,
the last-7-days activity mix), hands them to the pure planner
(:func:`app.engine.planner.plan_session`, T1), attaches NUMERIC zone
targets by REUSING the zone-building logic ``get_zones`` already uses, and
references a real library workout through the T2 adapter. The tool never
computes a decision of its own: the engine decides, the model explains.

Contract pinned here:
- ``ok``: every number in the payload is an engine output verbatim (the
  zone bounds are the pure zone functions over the stored thresholds; the
  reason is the planner's sentence, unaltered).
- ``insufficient_data``: ``missing`` names exactly what to connect or wait
  for — never a fabricated session.
- an unknown sport is an ARGUMENT error naming the accepted values, the
  same contract as the other tools.
- the imbalance rule (planner row 4) fires: the tool resolves the CONCRETE
  other sport (least share of the last 7 days) — the piece T1 deliberately
  left to the caller.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.core.settings import Settings
from app.db.models import ActivityRow
from app.engine.planner import plan_session
from tests.agent.test_agent_tools import (
    COMPUTED_AT,
    ENGINE_VERSION,
    make_activity,
    make_daily_load,
    make_profile,
    make_readiness_snapshot,
    real_today_local,
    seed,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tool_settings() -> Settings:
    """Settings pinned for tool tests, with the LTHR owner configuration set:
    the planner gates zone-bearing sessions on FTP AND LTHR being known."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        engine_version="settings-fallback-1.0",
        athlete_lthr_bpm=169.0,
    )


@pytest.fixture
async def tool_db(
    db_engine: AsyncEngine, tool_settings: Settings
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    """Point the tools' session and settings seams at the test fixtures."""
    factory: async_sessionmaker[Any] = async_sessionmaker(
        db_engine, expire_on_commit=False
    )
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(tool_settings)
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


def activity_on(index: int, day: dt.date, hour: int, sport: str) -> ActivityRow:
    """An activity row seeded relative to the REAL today (the tool's
    default day), so the 7-day window cannot drift past fixed dates."""
    return make_activity(
        index + 1,
        start=dt.datetime.combine(day, dt.time(hour), tzinfo=dt.UTC),
        sport=sport,
    )


# ---------------------------------------------------------------------------
# ok: the payload is the engine's decision, verbatim
# ---------------------------------------------------------------------------


class TestPlanWorkoutOk:
    async def test_ok_payload_is_the_engine_decision_verbatim(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        today = real_today_local()
        async with tool_db() as session:
            await seed(
                session,
                make_daily_load(today, ctl=50.0, atl=45.0, tsb=5.0, tss=90.0),
            )
            await seed(session, make_readiness_snapshot(today))
            await seed(session, make_profile())

        result = await tools.plan_workout(sport="ride")

        assert result["status"] == "ok"
        planned = result["session"]
        assert planned["sport"] == "ride"
        assert planned["kind"] == "quality"
        assert planned["session_type"] == "quality session"
        assert planned["duration_s"] is None
        # The numeric zone target IS the engine's power_zones output over
        # the stored FTP (210 W), verbatim — the same builder get_zones uses.
        [z3] = planned["zones"]
        assert z3["key"] == "Z3"
        assert z3["min_value"] == pytest.approx(76.0 * 210.0 / 100.0)
        assert z3["max_value"] == pytest.approx(91.0 * 210.0 / 100.0)
        # The reason is the planner's sentence, UNALTERED.
        engine = plan_session(
            readiness_suggests_reduce_intensity=False,
            tsb=5.0,
            thresholds_configured=True,
            sport_share=None,
            requested_sport="ride",
        )
        assert result["reason"] == engine.reason
        # A real library reference for the decided session (quality Z3 ride).
        assert result["library_workout"] == {"id": "cycling-E3", "name": "E3"}
        # Provenance from the persisted snapshot row (§6).
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"] == COMPUTED_AT.isoformat()
        # Coverage states what backed the answer.
        assert result["coverage"]["readiness_snapshot"] is True
        assert result["coverage"]["daily_load_combined"] is True

    async def test_imbalance_rule_resolves_the_concrete_other_sport(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        today = real_today_local()
        async with tool_db() as session:
            await seed(
                session,
                make_daily_load(today, ctl=50.0, atl=45.0, tsb=5.0, tss=90.0),
            )
            await seed(session, make_readiness_snapshot(today))
            await seed(session, make_profile())
            # 5 rides vs 1 run in the last 7 days: the dominant share is
            # 5/6 > 0.70, so the planner prefers the OTHER sport and the
            # tool must name it — the least-share sport of the window.
            for index, sport in enumerate(["Ride"] * 5 + ["Run"]):
                await seed(session, activity_on(index, today, 8 + index, sport))

        result = await tools.plan_workout()

        assert result["status"] == "ok"
        planned = result["session"]
        assert planned["sport"] == "run"
        # Run HR zones over the configured LTHR (169 bpm), the get_zones builder.
        # Z3's effective max is the next printed boundary (95%) under the
        # documented continuous-partition gap rule — the engine's own number.
        [z3] = planned["zones"]
        assert z3["key"] == "Z3"
        assert z3["min_value"] == pytest.approx(90.0 * 169.0 / 100.0)
        assert z3["max_value"] == pytest.approx(95.0 * 169.0 / 100.0)
        # The reason is the planner's row-4 sentence, with its numbers.
        engine = plan_session(
            readiness_suggests_reduce_intensity=False,
            tsb=5.0,
            thresholds_configured=True,
            sport_share=5.0 / 6.0,
            requested_sport=None,
        )
        assert result["reason"] == engine.reason
        # The curated library has cycling/gym content only: a run session
        # references nothing (no invented content).
        assert "library_workout" not in result


# ---------------------------------------------------------------------------
# insufficient_data: an explicit result naming what is missing (S5)
# ---------------------------------------------------------------------------


class TestPlanWorkoutInsufficientData:
    async def test_empty_database_names_what_to_connect_or_wait_for(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plan_workout()

        assert result["status"] == "insufficient_data"
        # No session is ever fabricated from incomplete evidence.
        assert "session" not in result
        assert "library_workout" not in result
        missing = " ".join(result["missing"])
        assert "readiness" in missing
        assert "TSB" in missing
        # The detail is the planner's sentence naming the gaps.
        assert result["detail"].startswith("No session can be planned yet")

    async def test_unset_thresholds_are_named_for_zone_bearing_sessions(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        # Readiness and load exist, so a quality session is decided — but
        # without FTP and LTHR its zones cannot be expressed numerically,
        # and the planner reports exactly the two anchors to set.
        today = real_today_local()
        async with tool_db() as session:
            await seed(
                session,
                make_daily_load(today, ctl=50.0, atl=45.0, tsb=5.0, tss=90.0),
            )
            await seed(session, make_readiness_snapshot(today))

        result = await tools.plan_workout(sport="ride")

        assert result["status"] == "insufficient_data"
        assert "session" not in result
        missing = " ".join(result["missing"])
        assert "athlete_ftp_w" in missing and "athlete_lthr_bpm" in missing


# ---------------------------------------------------------------------------
# error contracts
# ---------------------------------------------------------------------------


class TestPlanWorkoutErrors:
    async def test_unknown_sport_is_an_error_naming_the_accepted_values(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plan_workout(sport="curling")

        assert result["status"] == "error"
        assert "curling" in result["detail"]
        # The accepted set is the engine's own vocabulary.
        assert "ride" in result["detail"]
        assert "weighttraining" in result["detail"]

    async def test_unparseable_day_is_an_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plan_workout(day="nonsense")

        assert result["status"] == "error"
        assert "YYYY-MM-DD" in result["detail"]


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


class TestPlanWorkoutRegistration:
    def test_plan_workout_is_in_tool_list(self) -> None:
        names = {
            t.tool_spec["name"] if hasattr(t, "tool_spec") else t.__name__
            for t in tools.tool_list()
        }
        assert "plan_workout" in names
