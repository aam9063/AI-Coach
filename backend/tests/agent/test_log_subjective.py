"""WA-6 (write half) RED tests: the ``log_subjective`` tool (§9.3).

Contract pinned here:

- **Owner input, not engine output**: the athlete's own daily report
  (RPE, fatigue, soreness, notes) becomes a readiness SIGNAL (§7.4's
  "subjective fatigue" context signal). The row is owner-entered input
  data (the ``activity.rpe`` LOAD-12 precedent), so it carries no
  ``engine_version`` — but the tool RESULT keeps the typed contract of
  the read tools: ``status`` ok/error, ``computed_at``,
  ``engine_version`` and ``coverage``.
- **Idempotent per day**: the row is unique per ``(athlete_id, date)`` —
  logging twice the same day UPDATES the row, never duplicates it.
- **Readiness integration**: logging triggers the readiness recompute
  THROUGH the service (``app.services.readiness``), so a reported
  fatigue reaches the assessment immediately: TSB very negative +
  reported fatigue yield TWO agreeing adverse signals where before
  there was one (the whole point of this tool).
- **Never a silent write** (LOAD-12 rule): an out-of-range or empty
  report is an explicit ``error`` result naming the accepted values and
  the rejected one — nothing is stored.
- ``tool_list()`` grows by exactly this one tool.

DB-backed tests run on the dedicated test database and skip cleanly
without Postgres (see ``tests/conftest.py``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import tools
from app.core.settings import Settings
from app.db.engine_readiness_config import readiness_constants_from_settings
from app.db.models import DailyLoadRow, SubjectiveLogRow
from app.services.readiness import recompute_readiness

pytestmark = pytest.mark.anyio

ENGINE_VERSION = "test-engine-1.0"
COMPUTED_AT = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)


def real_today_local() -> date:
    """Today in the owner's timezone — what the tool logs against."""
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
    factory: async_sessionmaker[Any] = async_sessionmaker(
        db_engine, expire_on_commit=False
    )
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(tool_settings)
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


async def seed(session: AsyncSession, row: Any) -> None:
    session.add(row)
    await session.commit()


def make_daily_load(day: date, *, tsb: float) -> DailyLoadRow:
    """One combined daily_load row (the TSB context input of readiness)."""
    return DailyLoadRow(
        athlete_id=1, date=day, sport="combined", tss=0.0,
        ctl=40.0, atl=55.0, tsb=tsb,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


async def log_rows(factory: async_sessionmaker[Any]) -> list[SubjectiveLogRow]:
    async with factory() as session:
        rows = (
            (await session.execute(select(SubjectiveLogRow).order_by(SubjectiveLogRow.id)))
            .scalars()
            .all()
        )
    return list(rows)


# ---------------------------------------------------------------------------
# The readiness difference a subjective report makes (§7.4)
# ---------------------------------------------------------------------------

class TestLogSubjectiveReachesReadiness:
    async def test_reported_fatigue_makes_tsb_and_subjective_agree(
        self, tool_db: async_sessionmaker[Any], tool_settings: Settings
    ) -> None:
        """The flagship outcome: TSB very negative (one adverse signal)
        + a reported fatigue (second adverse signal) → the multi-signal
        warning rule now fires where before it did not (§7.4)."""
        today = real_today_local()
        async with tool_db() as session:
            await seed(session, make_daily_load(today, tsb=-15.0))
            # BEFORE state: readiness computed without the report.
            await recompute_readiness(
                session,
                window_end=today,
                days=1,
                engine_version=ENGINE_VERSION,
                **readiness_constants_from_settings(tool_settings),
            )
            await session.commit()

        before = await tools.get_readiness(today.isoformat())
        assert before["status"] == "ok"
        assert before["agreement_count"] == 1
        assert before["suggest_reduce_intensity"] is False
        assert any("TSB" in reason for reason in before["reasons"])

        result = await tools.log_subjective(fatigue=7)

        assert result["status"] == "ok"
        assert result["date"] == today.isoformat()
        # The result states WHAT was recorded...
        assert result["recorded"] == {
            "rpe": None, "fatigue": 7, "soreness": None, "notes": None,
        }
        # ...and WHAT it changes for readiness.
        assert result["readiness"]["status"] == "updated"
        assert result["readiness"]["agreement_count"] == 2
        assert result["readiness"]["suggest_reduce_intensity"] is True
        assert "subjective_fatigue" in result["readiness"]["adverse_signal_keys"]
        assert result["readiness"]["subjective_fatigue_reported"] is True
        # The typed contract of the read tools (provenance + coverage).
        assert result["engine_version"] == tool_settings.engine_version
        assert "computed_at" in result

        after = await tools.get_readiness(today.isoformat())
        assert after["agreement_count"] == 2
        assert after["suggest_reduce_intensity"] is True
        assert any("fatigue" in reason.lower() for reason in after["reasons"])

    async def test_report_without_tsb_row_is_recorded_with_explicit_readiness_state(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        """No daily_load row for the day → the report IS stored, but the
        readiness recompute cannot include it (TSB input missing); the
        result says so explicitly, naming what is missing and how to get
        it — never a fabricated snapshot."""
        result = await tools.log_subjective(fatigue=5, notes="cansado")

        assert result["status"] == "ok"
        assert result["readiness"]["status"] == "not_assessed"
        assert "daily_load" in result["readiness"]["detail"]
        assert "python -m app.db.daily_load" in result["readiness"]["detail"]
        rows = await log_rows(tool_db)
        assert len(rows) == 1  # the report itself was stored


# ---------------------------------------------------------------------------
# Idempotency: one row per (athlete, date)
# ---------------------------------------------------------------------------

class TestLogSubjectiveIdempotency:
    async def test_logging_twice_the_same_day_updates_the_row(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        await tools.log_subjective(fatigue=6)
        await tools.log_subjective(rpe=8, notes="día duro")

        rows = await log_rows(tool_db)
        assert len(rows) == 1
        assert rows[0].athlete_id == 1
        assert rows[0].fatigue == 6
        assert rows[0].rpe == 8
        assert rows[0].notes == "día duro"

    async def test_recorded_date_is_today_in_the_owner_timezone(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        await tools.log_subjective(fatigue=4)
        rows = await log_rows(tool_db)
        assert rows[0].date == real_today_local()


# ---------------------------------------------------------------------------
# Validation: never a silent write (LOAD-12 rule)
# ---------------------------------------------------------------------------

class TestLogSubjectiveValidation:
    async def test_out_of_range_rpe_is_an_error_naming_the_accepted_values(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.log_subjective(rpe=12)
        assert result["status"] == "error"
        assert "12" in result["detail"]
        assert "1-10" in result["detail"]
        assert await log_rows(tool_db) == []

    async def test_out_of_range_fatigue_is_an_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.log_subjective(fatigue=0)
        assert result["status"] == "error"
        assert "0" in result["detail"]
        assert "1-10" in result["detail"]
        assert await log_rows(tool_db) == []

    async def test_out_of_range_soreness_is_an_error(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.log_subjective(soreness=11)
        assert result["status"] == "error"
        assert "11" in result["detail"]
        assert "1-10" in result["detail"]
        assert await log_rows(tool_db) == []

    async def test_empty_report_is_an_error_even_with_notes(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.log_subjective(notes="solo una nota")
        assert result["status"] == "error"
        # Names the accepted values: at least one of the 1-10 fields.
        assert "rpe" in result["detail"]
        assert "fatigue" in result["detail"]
        assert "soreness" in result["detail"]
        assert await log_rows(tool_db) == []
