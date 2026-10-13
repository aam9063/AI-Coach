"""RED tests: an invalid sport is an ERROR naming the accepted values.

The live evidence (same turn as the disclosure defect): the owner's agent
called ``get_load_status(sport="tunnel")`` and the tool answered
``status="insufficient_data"`` with "no daily_load rows for sport
'tunnel'" — so the agent told the owner to connect a device when the real
problem was that "tunnel" is not a sport. Missing data and an invalid
argument are different failures (§9.3): the invalid argument must return
``status="error"`` naming the accepted values, so the agent sends the
owner to fix the RIGHT thing.

The accepted set is DERIVED from the engine's own sport vocabulary
(:data:`app.engine.load._KNOWN_SPORTS`) — never a second hardcoded list
that could drift from the engine. The same contract applies to every tool
that accepts a sport, so the registry is consistent.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.core.settings import Settings
from tests.agent.test_agent_tools import (
    make_daily_load,
    make_weekly,
    real_today_local,
    seed,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tool_settings() -> Settings:
    return Settings(_env_file=None, engine_version="settings-fallback-1.0")  # type: ignore[call-arg]


@pytest.fixture
async def tool_db(
    db_engine: AsyncEngine, tool_settings: Settings
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    factory: async_sessionmaker[Any] = async_sessionmaker(db_engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(tool_settings)
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


class TestAcceptedSportSetIsDerivedFromTheEngine:
    def test_registry_set_is_the_engine_vocabulary_itself(self) -> None:
        from app.engine import load as engine_load

        # Identity: the tool registry does NOT keep a second list — it
        # re-exports the engine's closed sport vocabulary, so the two
        # cannot drift apart.
        assert tools.ENGINE_KNOWN_SPORTS is engine_load._KNOWN_SPORTS

    def test_the_accepted_set_is_the_real_engine_sport_vocabulary(self) -> None:
        # The real owner-facing sports (engine families: cycling, running,
        # swimming, walking, strength) are all accepted...
        assert {
            "ride", "virtualride", "gravelride", "mountainbikeride", "ebikeride",
            "run", "trailrun", "treadmillrun", "virtualrun",
            "swim",
            "walk", "hike", "snowshoe",
            "weighttraining", "strengthworkout", "workout",
        } <= tools.ENGINE_KNOWN_SPORTS
        # ...and the sport from the live defect is NOT one of them.
        assert "tunnel" not in tools.ENGINE_KNOWN_SPORTS


class TestGetLoadStatusSportValidation:
    async def test_unknown_sport_is_an_error_naming_the_accepted_values(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_load_status("7d", sport="tunnel")

        assert result["status"] == "error"
        assert "tunnel" in result["detail"]
        for accepted in sorted(tools.ENGINE_KNOWN_SPORTS):
            assert accepted in result["detail"]

    async def test_unknown_sport_is_never_reported_as_missing_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        """The defect: a bad sport must not send the owner to connect a
        device. It is an argument error, not insufficient data."""
        result = await tools.get_load_status("7d", sport="tunnel")

        assert result["status"] != "insufficient_data"
        assert "connect your device" not in result["detail"].lower()
        assert "python -m app.db.daily_load" not in result["detail"]

    async def test_unknown_sport_validation_is_case_insensitive(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_load_status("7d", sport="Tunnel")
        assert result["status"] == "error"

    async def test_valid_sport_keeps_its_current_behaviour(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        today = real_today_local()
        async with tool_db() as session:
            await seed(session, make_daily_load(today, ctl=50.0, atl=70.0, tsb=-20.0,
                                                sport="ride"))

        result = await tools.get_load_status("7d", sport="ride")
        assert result["status"] == "ok"
        assert result["coverage"]["sport"] == "ride"

    async def test_valid_sport_matching_is_case_insensitive(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        """Strava-style 'Ride' capitalisation keeps working."""
        today = real_today_local()
        async with tool_db() as session:
            await seed(session, make_daily_load(today, ctl=50.0, atl=70.0, tsb=-20.0,
                                                sport="ride"))

        result = await tools.get_load_status("7d", sport="Ride")
        assert result["status"] == "ok"

    async def test_omitting_the_sport_keeps_the_combined_series(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        today = real_today_local()
        async with tool_db() as session:
            await seed(session, make_daily_load(today, ctl=50.0, atl=70.0, tsb=-20.0,
                                                sport="combined"))

        result = await tools.get_load_status("7d")
        assert result["status"] == "ok"
        assert result["coverage"]["sport"] == "combined"


class TestGetIntensityDistributionSportValidation:
    async def test_unknown_sport_is_an_error_naming_the_accepted_values(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_intensity_distribution(4, sport="tunnel")

        assert result["status"] == "error"
        assert "tunnel" in result["detail"]
        for accepted in sorted(tools.ENGINE_KNOWN_SPORTS):
            assert accepted in result["detail"]

    async def test_unknown_sport_is_never_reported_as_missing_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_intensity_distribution(4, sport="tunnel")

        assert result["status"] != "insufficient_data"
        assert "python -m app.db.engine_outputs" not in result["detail"]

    async def test_valid_sport_keeps_its_current_behaviour(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        current_year, current_week, _ = real_today_local().isocalendar()
        async with tool_db() as session:
            await seed(session, make_weekly(current_year, current_week, sport="run"))

        result = await tools.get_intensity_distribution(2, sport="run")
        assert result["status"] == "ok"
        assert all(w["sport"] == "run" for w in result["weeks"])


class TestGetZonesSportContract:
    """get_zones already rejects unknown sports; the contract is pinned as
    part of the registry-wide consistency check — and a sport the ENGINE
    knows but that has no zone table is still an error naming the
    zone-capable sports."""

    async def test_unknown_sport_names_the_zone_capable_sports(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_zones("tunnel")

        assert result["status"] == "error"
        for accepted in ("ride", "bike", "run", "swim"):
            assert accepted in result["detail"]

    async def test_engine_known_sport_without_a_zone_table_is_still_an_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_zones("walk")

        assert result["status"] == "error"
        for accepted in ("ride", "bike", "run", "swim"):
            assert accepted in result["detail"]

    async def test_error_carries_provenance(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_load_status("7d", sport="tunnel")
        assert result["engine_version"]
        assert result["computed_at"]
