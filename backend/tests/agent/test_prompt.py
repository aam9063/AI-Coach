"""WA-7 RED tests: the insufficient-data rule in the system prompt.

Two layers, matching how the project already pins prompt content:

- **Content pinned through the real loop**: the ``FakeModel`` records the
  ``system_prompts`` it was invoked with (the same mechanism
  ``tests/agent/test_fake_model_loop.py`` uses), so the tests assert the
  prompt the model ACTUALLY receives — not just the module constant.
- **One fake-model end-to-end scenario**: a tool returns
  ``insufficient_data`` inside the real pipeline and the reply must
  contain the missing-data statement (what is missing, how to get it,
  the coverage) — and never an invented zero.

Pinned contract (§9.3, §3):

1. When a tool reports ``insufficient_data`` (or any non-``ok`` status),
   the agent states WHAT is missing and HOW to get it (the tool's
   ``detail`` names both) and cites the ``coverage`` the tool returned.
2. The agent NEVER emits a zero or an estimate in place of missing data.
3. Units are stated (metric: watts, bpm, hours, km, m/s; TSS points).
4. The engine decides the numbers; the model only explains them (§3).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from strands import Agent

from app.agent import tools
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.agent.prompt import SYSTEM_PROMPT
from app.core.settings import Settings
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMwa7ss000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"


async def delivered_prompt() -> str:
    """The system prompt the REAL Strands loop hands to the model, with
    line-wrapping collapsed so content assertions are robust to how the
    text is wrapped."""
    import re

    model = FakeModel(["ok"])
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
    agent("hola")
    delivered = model.system_prompts[0]
    assert delivered is not None, "the loop must deliver a non-empty system prompt"
    return re.sub(r"\s+", " ", delivered)


class TestInsufficientDataRuleInPrompt:
    async def test_rule_tells_the_model_what_to_do(self) -> None:
        prompt = await delivered_prompt()

        # The rule keys on the tool statuses the contract defines.
        assert "insufficient_data" in prompt
        assert "ok" in prompt
        # Say WHAT is missing and HOW to get it (§9.3).
        assert "qué falta" in prompt.lower()
        assert "cómo obtenerlo" in prompt.lower()
        # Cite the coverage the tool returned.
        assert "coverage" in prompt.lower() or "cobertura" in prompt.lower()

    async def test_never_a_zero_or_an_estimate_in_place_of_missing_data(
        self,
    ) -> None:
        prompt = await delivered_prompt()
        assert "cero" in prompt.lower()
        assert "estim" in prompt.lower()  # estimar/estimación

    async def test_units_and_engine_decides_are_stated(self) -> None:
        prompt = await delivered_prompt()
        # §3: the engine decides, the model only explains.
        assert "motor" in prompt.lower()
        # Units (metric, §14): the ones the tools actually return.
        assert "vatios" in prompt.lower()  # watts (bike power)
        assert "pulsaciones" in prompt.lower()  # bpm (heart rate)
        assert "kilómetro" in prompt.lower()  # distance (km)


class TestInsufficientDataEndToEnd:
    @pytest.fixture
    async def session_factory(
        self,
    ) -> AsyncGenerator[async_sessionmaker[Any], None]:
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

    async def test_reply_states_what_is_missing_and_how_to_get_it(
        self, session_factory: async_sessionmaker[Any]
    ) -> None:
        """The scripted model calls ``get_load_status`` against an empty
        DB: the tool returns ``insufficient_data`` and the final reply
        (the one the model produces after SEEING that result) must state
        what is missing, how to get it and the coverage — never a zero."""
        missing_reply = (
            "Hoy no puedo darte tu CTL/ATL/TSB: falta el cálculo de carga. "
            "No hay datos de actividades en los últimos 7 días (cobertura: "
            "0 de 7 días). Conecta tu dispositivo para que las actividades "
            "lleguen a Intervals.icu y ejecuta "
            "python -m app.db.daily_load para calcularlos."
        )
        model = FakeModel(
            [
                {"name": "get_load_status", "input": {"date_range": "7d"}},
                missing_reply,
            ]
        )

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
        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": "¿cómo estoy?"},
            settings=Settings(_env_file=None, llm_provider="fake"),  # type: ignore[call-arg]
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
        )

        # The loop handed the insufficient_data RESULT back to the model
        # before the final answer: the reply rests on real tool evidence.
        second_invocation = model.invocations[1]["messages"]
        tool_results = [
            block["toolResult"]
            for message in second_invocation
            for block in message["content"]
            if "toolResult" in block
        ]
        assert tool_results, "the model must see the tool result"
        result_json = json.loads(tool_results[0]["content"][0]["text"])
        assert result_json["status"] == "insufficient_data"
        assert "daily_load" in result_json["detail"]
        assert "python -m app.db.daily_load" in result_json["detail"]
        assert result_json["coverage"]["days_with_data"] == 0

        # The reply contains the missing-data statement.
        reply = twilio.messages.calls[0]["body"]
        assert "falta" in reply.lower()
        assert "intervals.icu" in reply.lower()
        assert "python -m app.db.daily_load" in reply
        # ...cites the coverage the tool returned...
        assert "cobertura" in reply.lower()
        # ...and never substitutes a zero for the missing data.
        assert "CTL 0" not in reply and "TSB 0" not in reply
