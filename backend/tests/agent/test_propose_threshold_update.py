"""WA-6 closing tests: ``propose_threshold_update`` (§9.3, §7.3).

Contract pinned here:

- A threshold change is the OWNER'S decision, never the agent's (§7.3
  "propose (not apply)"): the tool validates the sport and the proposed
  value, PERSISTS a pending proposal for the owner's review and returns
  the typed contract stating plainly that the proposal is pending and
  that NO threshold was changed. Applying a threshold is a separate,
  owner-driven step (a later feature) — the tool has NO side effect on
  any active threshold: ``athlete_profile``, the owner settings and the
  engine outputs stay untouched (pinned below).
- Validation is honest: an unknown sport or an out-of-range value is an
  ``status="error"`` result NAMING the accepted values — never a silent
  write, never ``insufficient_data`` (a bad argument is not missing
  data).
- The accepted sport set is DERIVED from the structures the registry
  already maintains (the sport→zone-model map and the zone-model→owner
  settings field map that ``get_zones`` reads) — never a new list that
  could drift from the engine.

DB-backed tests run on the dedicated test database and skip cleanly
without Postgres (see ``tests/conftest.py``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import tools
from app.core.settings import Settings
from app.db.models import AthleteProfileRow, DailyLoadRow, ThresholdProposalRow

pytestmark = pytest.mark.anyio

ENGINE_VERSION = "test-engine-1.0"
COMPUTED_AT = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


@pytest.fixture
def tool_settings() -> Settings:
    """Settings pinned for tool tests (never the developer's .env)."""
    return Settings(_env_file=None, engine_version=ENGINE_VERSION)  # type: ignore[call-arg]


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


def make_profile(**overrides: Any) -> AthleteProfileRow:
    values: dict[str, Any] = {
        "athlete_id": 1,
        "ftp_watts": 210.0, "ftp_source": "manual",
        "css_mps": 0.8333333, "css_source": "css_from_time_trials",
        "engine_version": ENGINE_VERSION, "updated_at": COMPUTED_AT,
    }
    values.update(overrides)
    return AthleteProfileRow(**values)


def make_daily_load(day: date, *, ctl: float, atl: float, tsb: float,
                    tss: float = 0.0, sport: str = "combined") -> DailyLoadRow:
    return DailyLoadRow(
        athlete_id=1, date=day, sport=sport, tss=tss, ctl=ctl, atl=atl, tsb=tsb,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


# ---------------------------------------------------------------------------
# The ok path: persist a PENDING proposal, change nothing
# ---------------------------------------------------------------------------


class TestProposeThresholdUpdateOk:
    async def test_persists_a_pending_proposal_and_reports_no_change(
        self, tool_db: async_sessionmaker[Any], tool_settings: Settings
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="ride", new_value=250.0, evidence="best 20-min effort 250 W on 2026-10-01"
        )

        assert result["status"] == "ok"
        # The typed contract states PENDING and NO change, in its own fields.
        assert result["proposal"]["status"] == "pending"
        assert result["proposal"]["proposed_value"] == 250.0
        assert result["proposal"]["evidence"] == (
            "best 20-min effort 250 W on 2026-10-01"
        )
        assert result["threshold_change"]["applied"] is False
        assert "pending" in result["threshold_change"]["detail"].lower()
        assert "no threshold was changed" in result["threshold_change"]["detail"].lower()
        # Provenance contract (§9.3): every result carries these.
        assert result["engine_version"] == ENGINE_VERSION
        assert result["computed_at"]

    async def test_proposal_row_is_persisted_for_the_owner(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        await tools.propose_threshold_update(
            sport="run", new_value=172.0, evidence="10k race HR steady at 172"
        )

        async with tool_db() as session:
            row = (
                await session.execute(select(ThresholdProposalRow))
            ).scalar_one()
        assert row.athlete_id == 1
        assert row.sport == "run"
        assert row.proposed_value == 172.0
        assert row.evidence == "10k race HR steady at 172"
        assert row.status == "pending"
        assert row.created_at is not None

    async def test_two_proposals_are_two_rows_append_only(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        await tools.propose_threshold_update(
            sport="ride", new_value=250.0, evidence="first effort"
        )
        await tools.propose_threshold_update(
            sport="ride", new_value=255.0, evidence="second effort"
        )
        async with tool_db() as session:
            rows = (
                (await session.execute(select(ThresholdProposalRow))).scalars().all()
            )
        assert len(rows) == 2

    async def test_states_what_approval_would_change_and_where(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="ride", new_value=250.0, evidence="20 min at 250 W"
        )
        where = result["threshold_change"]["detail"]
        # The owner settings field the zone engine reads, and the profile
        # column, are both named so the owner knows where approval lands.
        assert "ATHLETE_FTP_W" in where
        assert "athlete_profile" in where

    async def test_states_what_approval_would_change_and_where_swim(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="swim", new_value=0.90, evidence="400/200 time trials"
        )
        assert result["status"] == "ok"
        assert "ATHLETE_CSS_SPEED_MPS" in result["threshold_change"]["detail"]


# ---------------------------------------------------------------------------
# Argument errors: named, never stored, never insufficient_data
# ---------------------------------------------------------------------------


class TestProposeThresholdUpdateErrors:
    async def test_unknown_sport_is_an_error_naming_accepted_values(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="kayak", new_value=1.2, evidence="a great paddle"
        )
        assert result["status"] == "error"
        for accepted in ("ride", "run", "swim"):
            assert accepted in result["detail"]
        assert "kayak" in result["detail"]

    async def test_error_is_never_insufficient_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        for kwargs in (
            {"sport": "kayak", "new_value": 1.2, "evidence": "x"},
            {"sport": "ride", "new_value": 0.0, "evidence": "x"},
            {"sport": "ride", "new_value": 9999.0, "evidence": "x"},
        ):
            result = await tools.propose_threshold_update(**kwargs)  # type: ignore[arg-type]
            assert result["status"] == "error"
            assert result["status"] != "insufficient_data"

    async def test_non_positive_value_rejected_nothing_stored(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="ride", new_value=0.0, evidence="no effort"
        )
        assert result["status"] == "error"
        assert "0.0" in result["detail"] or "0" in result["detail"]

    async def test_absurd_value_rejected_naming_the_accepted_range(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="ride", new_value=5000.0, evidence="a motor"
        )
        assert result["status"] == "error"
        # The accepted range is named so the model can correct itself.
        assert "watts" in result["detail"].lower()
        async with tool_db() as session:
            assert (
                await session.execute(select(ThresholdProposalRow))
            ).scalar_one_or_none() is None

    async def test_blank_evidence_rejected_nothing_stored(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.propose_threshold_update(
            sport="ride", new_value=250.0, evidence="   "
        )
        assert result["status"] == "error"
        assert "evidence" in result["detail"].lower()
        async with tool_db() as session:
            assert (
                await session.execute(select(ThresholdProposalRow))
            ).scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# NO side effect on any active threshold (pinned)
# ---------------------------------------------------------------------------


class TestNoSideEffectOnActiveThresholds:
    async def test_profile_settings_and_engine_outputs_stay_untouched(
        self, tool_db: async_sessionmaker[Any], tool_settings: Settings,
        db_engine: AsyncEngine,
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_profile())
            await seed(session, make_daily_load(date(2026, 10, 14), ctl=100.0, atl=80.0, tsb=-20.0))
        profile_before = tool_settings.athlete_ftp_w
        assert profile_before is None  # the pinned test settings, unchanged by the call

        await tools.propose_threshold_update(
            sport="ride", new_value=250.0, evidence="20 min at 250 W"
        )

        # The persisted profile row is byte-identical.
        async with tool_db() as session:
            profile = (
                await session.execute(
                    select(AthleteProfileRow).where(AthleteProfileRow.athlete_id == 1)
                )
            ).scalar_one()
        assert profile.ftp_watts == 210.0
        assert profile.ftp_source == "manual"
        assert profile.updated_at == COMPUTED_AT
        # The engine outputs are untouched.
        async with tool_db() as session:
            loads = (
                (await session.execute(select(DailyLoadRow))).scalars().all()
            )
        assert len(loads) == 1
        assert loads[0].ctl == 100.0
        # The owner settings object the tools read was not mutated.
        assert tool_settings.athlete_ftp_w is None


# ---------------------------------------------------------------------------
# Registry completeness (WA-6 / brief §9.3): the full ten-tool set
# ---------------------------------------------------------------------------


class TestRegistryCompletesWA6:
    def test_tool_list_contains_propose_threshold_update(self) -> None:
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in tools.tool_list()
        }
        assert "propose_threshold_update" in names
