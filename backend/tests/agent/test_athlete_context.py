"""WA-8 (second half) RED tests: the per-turn athlete context block.

Pinned contract (§9.2; ODD task WA-8):

- The pipeline reads a COMPACT context block from the database
  (``athlete_profile`` — the value of record, with its provenance) and
  the owner configuration (settings) at TURN TIME and hands it to the
  model with the system prompt. Not a hardcoded blob, not a snapshot.
- A threshold that is not configured is reported AS MISSING — never
  defaulted, never guessed, never a silent zero (the same honesty rule
  the tools already follow, §9.3).
- Threshold resolution mirrors the tools (``get_zones``): the profile
  row wins with its own source; settings are the fallback with the
  ``owner_configuration`` provenance.
- The block stays SHORT: it carries identity, configured thresholds and
  units only — the numbers a reply needs come from the tools.

The pipeline e2e test asserts the context reaches the model: the
``FakeModel`` records the system prompt it was actually invoked with.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.context import build_athlete_context
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import AthleteProfileRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMctx00000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "llm_provider": "fake",
        "athlete_name": "Alberto",
        "athlete_ftp_w": None,
        "athlete_lthr_bpm": None,
        "athlete_hr_max_bpm": None,
        "athlete_hr_rest_bpm": None,
        "athlete_css_speed_mps": None,
        "athlete_threshold_run_speed_mps": None,
        "athlete_weight_kg": None,
        "engine_trimp_sex": "male",
    }
    values.update(overrides)
    return Settings(**values)


async def seed_profile(factory: async_sessionmaker[Any], **overrides: Any) -> None:
    values: dict[str, Any] = {
        "athlete_id": 1,
        "engine_version": "0.1.0",
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    async with factory() as session:
        session.add(AthleteProfileRow(**values))
        await session.commit()


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[Any], None]:
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; pipeline tests require the compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    yield factory
    tools.reset_session_factory_provider()
    await engine.dispose()


class TestAthleteContextBlock:
    async def test_configured_thresholds_are_reported_with_their_source(
        self,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """The profile row is the value of record: its FTP/CSS values and
        sources are reported; settings-only thresholds report their value."""
        await seed_profile(
            session_factory,
            ftp_watts=250.0,
            ftp_source="manual",
            css_mps=0.8333333,
            css_source="css_from_time_trials",
        )
        settings = make_settings(
            athlete_lthr_bpm=165.0,
            athlete_hr_max_bpm=190.0,
            athlete_hr_rest_bpm=55.0,
        )

        context = await build_athlete_context(session_factory, settings)

        assert "Alberto" in context  # identity (who the athlete is)
        assert "250" in context and "manual" in context
        assert "165" in context  # LTHR
        assert "190" in context  # max HR
        assert "55" in context  # resting HR
        assert "0.833" in context and "css_from_time_trials" in context
        # Units are stated with the values (the same units the tools use).
        assert "vatios" in context.lower() or "W" in context
        assert "ppm" in context
        assert "m/s" in context
        # Provenance mirrors the tools: the profile source, not
        # owner_configuration, when the profile holds the value.
        assert "owner_configuration" not in context

    async def test_owner_configuration_is_the_fallback_with_provenance(
        self,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """No profile row: the settings value is used and named as owner
        configuration (the same provenance the tools report)."""
        settings = make_settings(athlete_ftp_w=230.0)

        context = await build_athlete_context(session_factory, settings)

        assert "230" in context
        assert "owner_configuration" in context

    async def test_missing_thresholds_are_named_as_missing_never_defaulted(
        self,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """Every threshold configured nowhere is reported AS MISSING —
        all seven are named, and none is substituted by a default or a
        zero (the honesty rule the tools follow, §9.3)."""
        settings = make_settings()  # every threshold None; no profile

        context = await build_athlete_context(session_factory, settings)

        assert "no configurad" in context.lower()
        # All seven configurable thresholds are named.
        lowered = context.lower()
        for name in ("ftp", "lthr", "fc máx", "fc reposo", "css",
                     "carrera", "peso"):
            assert name in lowered, f"missing threshold {name!r} must be named"
        # Never defaulted or guessed: no fabricated value.
        assert "FTP 0" not in context
        assert "peso 0" not in lowered
        # The instruction to say it is missing (not to invent it) is there.
        assert "falta" in context.lower()
        assert "inventa" in context.lower() or "estimes" in context.lower()

    async def test_context_stays_short(self, session_factory: async_sessionmaker[Any]) -> None:
        """The block is context, not a data dump: a hard size cap keeps it
        from growing into one."""
        await seed_profile(session_factory, ftp_watts=250.0, ftp_source="manual")
        settings = make_settings(athlete_lthr_bpm=165.0, athlete_hr_max_bpm=190.0)

        context = await build_athlete_context(session_factory, settings)

        assert len(context) < 1200


class TestContextReachesTheModel:
    async def test_pipeline_hands_the_context_to_the_model(
        self,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """The system prompt the model ACTUALLY receives carries the
        athlete context, read from the DB at turn time."""
        await seed_profile(
            session_factory, ftp_watts=250.0, ftp_source="manual"
        )
        model = FakeModel(["Todo en orden."])

        class FakeTwilioMessages:
            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            def create(self, **kwargs: str) -> Any:
                self.calls.append(kwargs)
                return {"sid": "SMout"}

        class FakeTwilioClient:
            def __init__(self) -> None:
                self.messages = FakeTwilioMessages()

        twilio = FakeTwilioClient()
        from app.agent.session_store import DbSessionRepository
        from tests.dbsupport import test_database_url

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": "¿cómo estoy?"},
            settings=make_settings(athlete_name="Alberto"),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=DbSessionRepository(test_database_url()),
        )

        delivered = model.invocations[0]["system_prompt"]
        assert delivered is not None
        # The core prompt is still there...
        assert "Principios innegociables" in delivered
        # ...and the per-turn athlete context rides with it.
        assert "Contexto del atleta" in delivered
        assert "Alberto" in delivered
        assert "250" in delivered  # the seeded profile FTP, read this turn
