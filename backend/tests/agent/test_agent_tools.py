"""WA-6 (read half) RED tests: the engine-backed read tools (§9.3).

Contract pinned here, per tool:

- **Thin wrappers only**: DB read (outside the pure engine) → engine call /
  persisted engine output → typed result. The tool NEVER computes a number
  itself (§3): every number in a result is an engine output (persisted
  ``daily_load`` / ``readiness_snapshot`` / ``weekly_intensity`` /
  ``session_durability`` rows, or the pure zone functions of
  :mod:`app.engine.zones` over the stored thresholds).
- **Provenance**: every result carries ``engine_version`` and
  ``computed_at`` taken from the persisted engine row that backs it (§6).
- **Data coverage**: every result states how many days/weeks/sessions backed
  it, so the agent can say what it is based on (WA-7 seed).
- **Insufficient data** (§9.3): a tool without the data it needs returns an
  EXPLICIT insufficient-data result naming WHAT is missing and HOW to get
  it — never a zero, never a guessed value.
- ``tool_list()`` remains the EXPLICIT list containing only these tools —
  never the SDK's directory loader, never ``strands-agents-tools``.

DB-backed tests run on the dedicated test database and skip cleanly without
Postgres (see ``tests/conftest.py``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import tools
from app.core.settings import Settings
from app.db.models import (
    ActivityRow,
    AthleteProfileRow,
    DailyLoadRow,
    ReadinessSnapshotRow,
    SessionDurabilityRow,
    WeeklyIntensityRow,
)

pytestmark = pytest.mark.anyio

ENGINE_VERSION = "test-engine-1.0"
COMPUTED_AT = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)
TODAY = date(2026, 10, 7)


def real_today_local() -> date:
    """Today in the owner's timezone — what the tools use as 'today' — so
    default-date and current-ISO-week tests cannot break on calendar drift."""
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


@pytest.fixture
def tool_settings() -> Settings:
    """Settings pinned for tool tests (never the developer's .env)."""
    return Settings(_env_file=None, engine_version="settings-fallback-1.0")  # type: ignore[call-arg]


@pytest.fixture
async def tool_db(
    db_engine: AsyncEngine, tool_settings: Settings
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    """Point the tools' session and settings seams at the test fixtures."""
    factory: async_sessionmaker[Any] = async_sessionmaker(db_engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(tool_settings)
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


async def seed(session: AsyncSession, row: Any) -> None:
    session.add(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Seed helpers (engine OUTPUT rows — what the tools read)
# ---------------------------------------------------------------------------

def make_daily_load(day: date, *, ctl: float, atl: float, tsb: float,
                    tss: float = 0.0, sport: str = "combined") -> DailyLoadRow:
    return DailyLoadRow(
        athlete_id=1, date=day, sport=sport, tss=tss, ctl=ctl, atl=atl, tsb=tsb,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


def make_profile(**overrides: Any) -> AthleteProfileRow:
    values: dict[str, Any] = {
        "athlete_id": 1,
        "ftp_watts": 210.0, "ftp_source": "manual",
        "css_mps": 0.8333333, "css_source": "css_from_time_trials",
        "engine_version": ENGINE_VERSION, "updated_at": COMPUTED_AT,
    }
    values.update(overrides)
    return AthleteProfileRow(**values)


def make_readiness_snapshot(day: date) -> ReadinessSnapshotRow:
    return ReadinessSnapshotRow(
        athlete_id=1, date=day,
        signals=[
            {"key": "hrv", "status": "ok", "observed": 4.17, "baseline": 4.1,
             "direction": "up", "confidence": "moderate"},
            {"key": "resting_hr", "status": "insufficient_data", "observed": None,
             "baseline": None, "direction": None, "confidence": None},
        ],
        adverse_signal_keys=["tsb_very_negative"],
        agreement_count=1, suggest_reduce_intensity=False,
        suggestion=None, reasons=["TSB is below the very-negative threshold."],
        tsb=-12.0, tsb_very_negative_below=-10.0,
        subjective_fatigue_reported=None, acwr=None,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


def make_activity(
    activity_id: int, *, start: datetime | None = None, sport: str = "Ride",
    name: str = "Morning ride", duration_s: int = 5400, distance_m: float = 40_000.0,
) -> ActivityRow:
    return ActivityRow(
        id=activity_id, source="intervals", source_id=f"i{activity_id}",
        type=sport, name=name,
        start_time=start or datetime(2026, 10, 6, 8, 0, tzinfo=UTC),
        duration_s=duration_s, distance_m=distance_m,
    )


def make_durability(activity_id: int, *, decoupling: float = 0.031) -> SessionDurabilityRow:
    return SessionDurabilityRow(
        activity_id=activity_id, sport="bike", status="ok",
        ef_first_half=0.98, ef_second_half=0.95,
        decoupling=decoupling, decoupling_pct=decoupling * 100.0,
        within_reference_band=True, reference_band=0.05,
        intensity_first_half=210.0, intensity_second_half=203.0,
        intensity_drift=0.033, max_intensity_drift=0.15,
        n_samples=5400, n_first_half=2700, n_second_half=2700,
        detail="", engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


def make_weekly(iso_year: int, iso_week: int, *, sport: str = "bike",
                status: str = "data", pcts: list[float] | None = None) -> WeeklyIntensityRow:
    if pcts is None:
        pcts = [75.0, 15.0, 10.0] if status == "data" else None
    seconds = (12000.0, 2400.0, 1600.0) if status == "data" else (0.0, 0.0, 0.0)
    return WeeklyIntensityRow(
        athlete_id=1, iso_year=iso_year, iso_week=iso_week,
        week_start=date.fromisocalendar(iso_year, iso_week, 1), sport=sport,
        status=status, z1_seconds=seconds[0], z2_seconds=seconds[1],
        z3_seconds=seconds[2], total_seconds=sum(seconds), percentages=pcts,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


# ---------------------------------------------------------------------------
# tool_list: the explicit registry
# ---------------------------------------------------------------------------

class TestToolList:
    def test_explicit_list_contains_exactly_the_engine_tools(self) -> None:
        listed = tools.tool_list()
        names = {t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"] for t in listed}
        assert names == {
            "get_load_status", "get_zones", "get_readiness",
            "get_activity_analysis", "get_intensity_distribution",
        }
        assert len(listed) == 5

    def test_no_directory_loader_and_no_vended_tools(self) -> None:
        listed_names = {
            t.tool_spec["name"] if hasattr(t, "tool_spec") else t.__name__
            for t in tools.tool_list()
        }
        # Nothing that executes on the host may ever enter the list.
        for forbidden in ("editor", "shell", "http_request", "load_tools_from_directory"):
            assert forbidden not in listed_names
        # Every listed tool is OURS: its implementation lives in app.agent.
        for listed in tools.tool_list():
            underlying = getattr(listed, "_tool_func", None) or listed
            assert getattr(underlying, "__module__", "app.agent.tools").startswith(
                "app.agent"
            )


# ---------------------------------------------------------------------------
# get_load_status
# ---------------------------------------------------------------------------

class TestGetLoadStatus:
    async def test_returns_engine_ctl_atl_tsb_from_persisted_rows(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            for offset in range(3):
                await seed(session, make_daily_load(
                    TODAY - timedelta(days=2 - offset),
                    ctl=40.0 + offset, atl=60.0 + offset, tsb=-20.0 + offset, tss=90.0,
                ))

        result = await tools.get_load_status("7d")

        assert result["status"] == "ok"
        # Latest day's engine values, verbatim from the persisted row.
        assert result["ctl"] == pytest.approx(42.0)
        assert result["atl"] == pytest.approx(62.0)
        assert result["tsb"] == pytest.approx(-18.0)
        assert result["date"] == TODAY.isoformat()
        # Provenance from the persisted engine row (§6).
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"] == COMPUTED_AT.isoformat()
        # Coverage: how many days backed the answer.
        assert result["coverage"]["days_with_data"] == 3
        assert result["coverage"]["window_days"] == 7

    async def test_sport_filter_reads_the_per_sport_rows(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_daily_load(TODAY, ctl=50.0, atl=70.0, tsb=-20.0,
                                                sport="ride"))
            await seed(session, make_daily_load(TODAY, ctl=45.0, atl=65.0, tsb=-20.0,
                                                sport="combined"))

        result = await tools.get_load_status("7d", sport="ride")

        assert result["status"] == "ok"
        assert result["ctl"] == pytest.approx(50.0)
        assert result["coverage"]["sport"] == "ride"

    async def test_empty_database_is_insufficient_data_never_zero(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_load_status("30d")

        assert result["status"] == "insufficient_data"
        assert "ctl" not in result and "tsb" not in result
        # Names WHAT is missing and HOW to get it (§9.3).
        assert "daily_load" in result["detail"]
        assert "python -m app.db.daily_load" in result["detail"]

    async def test_explicit_date_range_is_parsed(self, tool_db: async_sessionmaker[Any]) -> None:
        async with tool_db() as session:
            await seed(session, make_daily_load(date(2026, 9, 15), ctl=30.0, atl=50.0,
                                                tsb=-20.0))

        result = await tools.get_load_status("2026-09-01/2026-09-30")

        assert result["status"] == "ok"
        assert result["date"] == "2026-09-15"
        assert result["coverage"]["days_with_data"] == 1

    async def test_unparseable_range_is_an_error_not_a_guess(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_load_status("nonsense")
        assert result["status"] == "error"
        assert "7d" in result["detail"]


# ---------------------------------------------------------------------------
# get_zones
# ---------------------------------------------------------------------------

class TestGetZones:
    async def test_bike_power_zones_from_the_stored_ftp(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_profile())

        result = await tools.get_zones("ride")

        assert result["status"] == "ok"
        assert result["unit"] == "watts"
        # The zone bounds ARE the engine's power_zones output, verbatim.
        from app.engine.zones import power_zones

        engine_zones = power_zones(210.0)
        assert [z["key"] for z in result["zones"]] == [z.key for z in engine_zones]
        z2 = next(z for z in result["zones"] if z["key"] == "Z2")
        assert z2["min_value"] == pytest.approx(55.0 * 210.0 / 100.0)
        assert z2["max_value"] == pytest.approx(76.0 * 210.0 / 100.0)
        # Threshold provenance + row provenance.
        assert result["threshold"]["value"] == 210.0
        assert result["threshold"]["source"] == "manual"
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"] == COMPUTED_AT.isoformat()

    async def test_run_hr_zones_from_the_owner_lthr_configuration(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        # LTHR is owner configuration (§14), read through the settings seam.
        tools.set_settings_override(
            Settings(_env_file=None, athlete_lthr_bpm=169.0)  # type: ignore[call-arg]
        )
        async with tool_db() as session:
            await seed(session, make_profile(ftp_watts=None, ftp_source=None))

        result = await tools.get_zones("run")

        assert result["status"] == "ok"
        assert result["unit"] == "bpm"
        from app.engine.zones import hr_zones

        engine_zones = hr_zones("run", 169.0)
        assert [z["key"] for z in result["zones"]] == [z.key for z in engine_zones]
        z4 = next(z for z in result["zones"] if z["key"] == "Z4")
        assert z4["min_value"] == pytest.approx(95.0 * 169.0 / 100.0)

    async def test_swim_zones_as_pace_per_100m(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_profile())

        result = await tools.get_zones("swim")

        assert result["status"] == "ok"
        assert result["unit"] == "s_per_100m"
        assert len(result["zones"]) == 5  # the five CSS bands

    async def test_missing_threshold_is_insufficient_data_naming_the_fix(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_profile(ftp_watts=None, ftp_source=None))

        result = await tools.get_zones("bike")

        assert result["status"] == "insufficient_data"
        assert "FTP" in result["detail"] or "ftp" in result["detail"]
        assert "ATHLETE_FTP_W" in result["detail"]
        assert "zones" not in result

    async def test_missing_profile_is_insufficient_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_zones("swim")
        assert result["status"] == "insufficient_data"

    async def test_unknown_sport_is_an_error_naming_the_supported_ones(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_zones("curling")
        assert result["status"] == "error"
        assert "run" in result["detail"] and "swim" in result["detail"]


# ---------------------------------------------------------------------------
# get_readiness
# ---------------------------------------------------------------------------

class TestGetReadiness:
    async def test_returns_the_persisted_multi_signal_snapshot(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_readiness_snapshot(TODAY))

        result = await tools.get_readiness(TODAY.isoformat())

        assert result["status"] == "ok"
        assert len(result["signals"]) == 2
        assert result["agreement_count"] == 1
        assert result["suggest_reduce_intensity"] is False
        assert result["tsb"] == pytest.approx(-12.0)
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"] == COMPUTED_AT.isoformat()
        # Coverage: which signals backed the answer and which did not.
        assert result["coverage"]["signals_total"] == 2
        assert result["coverage"]["signals_insufficient"] == ["resting_hr"]

    async def test_default_date_is_today(self, tool_db: async_sessionmaker[Any]) -> None:
        async with tool_db() as session:
            await seed(session, make_readiness_snapshot(real_today_local()))
        result = await tools.get_readiness()
        assert result["status"] == "ok"

    async def test_no_snapshot_names_the_missing_inputs(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_readiness(TODAY.isoformat())

        assert result["status"] == "insufficient_data"
        assert "readiness" in result["detail"].lower()
        # What is missing: wellness data (HRV/sleep/resting HR)...
        assert "wellness" in result["detail"].lower()
        # ...and how to get it.
        assert "python -m app.db.engine_outputs" in result["detail"]
        assert "signals" not in result


# ---------------------------------------------------------------------------
# get_activity_analysis
# ---------------------------------------------------------------------------

class TestGetActivityAnalysis:
    async def test_last_activity_with_its_durability_engine_output(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_activity(1, start=datetime(2026, 10, 1, 8, 0, tzinfo=UTC)))
            await seed(session, make_activity(2))
            await seed(session, make_durability(2))

        result = await tools.get_activity_analysis("last")

        assert result["status"] == "ok"
        assert result["activity"]["id"] == 2
        assert result["activity"]["sport"] == "Ride"
        assert result["activity"]["duration_s"] == 5400
        # The durability numbers ARE the persisted engine outputs.
        assert result["durability"]["status"] == "ok"
        assert result["durability"]["decoupling"] == pytest.approx(0.031)
        assert result["durability"]["within_reference_band"] is True
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"] == COMPUTED_AT.isoformat()
        assert result["coverage"]["durability_samples"] == 5400

    async def test_explicit_activity_id(self, tool_db: async_sessionmaker[Any]) -> None:
        async with tool_db() as session:
            await seed(session, make_activity(7))
        result = await tools.get_activity_analysis("7")
        assert result["status"] == "ok"
        assert result["activity"]["id"] == 7

    async def test_no_activities_is_insufficient_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_activity_analysis("last")
        assert result["status"] == "insufficient_data"
        assert "Intervals.icu" in result["detail"]

    async def test_unknown_id_names_the_id(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_activity(1))
        result = await tools.get_activity_analysis("999")
        assert result["status"] == "insufficient_data"
        assert "999" in result["detail"]

    async def test_activity_without_durability_row_reports_it_explicitly(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_activity(3, sport="Swim"))
        result = await tools.get_activity_analysis("3")

        assert result["status"] == "ok"  # the activity summary exists
        assert result["durability"]["status"] == "insufficient_data"
        assert "python -m app.db.engine_outputs" in result["durability"]["detail"]
        assert result["coverage"]["durability_samples"] == 0


# ---------------------------------------------------------------------------
# get_intensity_distribution
# ---------------------------------------------------------------------------

class TestGetIntensityDistribution:
    async def test_returns_the_engine_weekly_percentages(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        current_year, current_week, _ = real_today_local().isocalendar()
        async with tool_db() as session:
            await seed(session, make_weekly(current_year, current_week - 1,
                                            pcts=[80.0, 10.0, 10.0]))
            await seed(session, make_weekly(current_year, current_week))

        result = await tools.get_intensity_distribution(4)

        assert result["status"] == "ok"
        assert len(result["weeks"]) == 2
        # The percentages are the ENGINE outputs, verbatim per week — the
        # tool never aggregates or recomputes them (§3).
        assert sorted(w["percentages"] for w in result["weeks"]) == [
            [75.0, 15.0, 10.0],
            [80.0, 10.0, 10.0],
        ]
        assert result["coverage"]["weeks_requested"] == 4
        assert result["coverage"]["weeks_with_data"] == 2
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"] == COMPUTED_AT.isoformat()

    async def test_sport_filter(self, tool_db: async_sessionmaker[Any]) -> None:
        current_year, current_week, _ = real_today_local().isocalendar()
        async with tool_db() as session:
            await seed(session, make_weekly(current_year, current_week, sport="run"))

        result = await tools.get_intensity_distribution(2, sport="run")
        assert result["status"] == "ok"
        assert all(w["sport"] == "run" for w in result["weeks"])

    async def test_no_rows_is_insufficient_data_naming_the_fix(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.get_intensity_distribution(4)

        assert result["status"] == "insufficient_data"
        assert "weekly_intensity" in result["detail"]
        assert "python -m app.db.engine_outputs" in result["detail"]

    async def test_only_no_data_weeks_is_insufficient_too(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        current_year, current_week, _ = real_today_local().isocalendar()
        async with tool_db() as session:
            await seed(session, make_weekly(current_year, current_week - 1, status="no_data"))

        result = await tools.get_intensity_distribution(4)
        assert result["status"] == "insufficient_data"

    async def test_invalid_weeks_is_an_error(self, tool_db: async_sessionmaker[Any]) -> None:
        result = await tools.get_intensity_distribution(0)
        assert result["status"] == "error"
