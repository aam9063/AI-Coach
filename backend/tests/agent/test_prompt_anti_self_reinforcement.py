"""Anti-self-reinforcement rules in the DELIVERED system prompt (live
finding, 2026-10-14).

Live evidence this pins: the owner asked three times how to compute his
maximum heart rate and got "FC max = 220 - edad" every time, while his
ingested guide says Tanaka. The session dump showed WHY: the recent
window held a stale FALSE tool result ("the evidence store is empty")
plus FOUR copies of the assistant's own wrong answer, so the model
stopped searching and copied itself. The durable rolling summary was
clean — the poison lives in the recent window, and the fix is prompt
rules that stop a conversation from reinforcing its own errors.

Three rules pinned here, in the system prompt the REAL Strands loop
hands the model (the delivered-prompt technique
``tests/agent/test_prompt.py`` uses — the ``FakeModel`` records the
system prompts it was invoked with):

1. The store CHANGES as the owner ingests sources, so a search result
   is NEVER carried forward: search again for each new question that
   needs a source, even if you searched earlier in this conversation.
2. What a tool returns NOW outranks the model's memory and its own
   earlier answers in this conversation; on contradiction the model
   corrects itself explicitly and says plainly it was wrong.
3. A tool result from an earlier turn is NEVER evidence for the current
   question.

The fake model cannot be forced to comply with a prompt rule, so these
tests pin WHAT THE MODEL RECEIVES, not model behaviour: real compliance
is only observable with the real model (stated in the task report, not
claimed here). The pipeline test additionally proves the rule survives
into the delivered prompt on a turn whose conversation memory already
contains an earlier reply answering the SAME question — the exact
poison-window shape of the live finding.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.agent.prompt import PROMPT_VERSION
from tests.agent.test_prompt import delivered_prompt
from tests.dbsupport import create_test_engine
from tests.dbsupport import test_database_url as _test_database_url

pytestmark = pytest.mark.anyio

TO = "whatsapp:+34600000000"
FROM = "whatsapp:+34600000001"


def _collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "")


def make_message(sid: str, body: str) -> dict[str, str]:
    return {"MessageSid": sid, "From": FROM, "To": TO, "Body": body}


def make_settings() -> Any:
    from app.core.settings import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="",
        twilio_auth_token="",
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


class TestRule1ReSearchEveryQuestion:
    async def test_delivered_prompt_says_a_search_result_is_never_carried_forward(
        self,
    ) -> None:
        prompt = (await delivered_prompt()).lower()

        # The store changes as sources are ingested...
        assert "cambia" in prompt
        # ...so a search result is NEVER carried forward...
        assert "nunca se arrastra" in prompt
        # ...and the model searches again for each new question that
        # needs a source, even if it already searched this conversation.
        assert "vuelve a llamar a search_evidence" in prompt
        assert "aunque ya hayas buscado" in prompt


class TestRule2ToolNowOutranksMemory:
    async def test_delivered_prompt_says_tool_now_outranks_memory_and_earlier_answers(
        self,
    ) -> None:
        prompt = (await delivered_prompt()).lower()

        # What a tool returns NOW outranks memory AND the model's own
        # earlier answers in this conversation.
        assert "lo que una herramienta devuelve ahora" in prompt
        assert "manda sobre tu memoria" in prompt
        assert "respuestas anteriores" in prompt
        # On contradiction: explicit self-correction, plainly wrong.
        assert "corrígete explícitamente" in prompt
        assert "te equivocaste" in prompt


class TestRule3NoEarlierToolResultAsEvidence:
    async def test_delivered_prompt_says_an_earlier_tool_result_is_never_evidence(
        self,
    ) -> None:
        prompt = (await delivered_prompt()).lower()

        assert "nunca uses el resultado de una herramienta de un turno anterior" in prompt
        assert "pregunta actual" in prompt


class TestExistingGuardsSurvive:
    async def test_honesty_guard_current_question_rule_and_no_citations_intact(
        self,
    ) -> None:
        """The three live-finding rules must not dilute what rule 4
        already pinned: the honesty guard, the current-question query
        rule, and the no-citations OWNER DECISION of 2026-10-14."""
        prompt = await delivered_prompt()
        lowered = prompt.lower()

        # Honesty guard (no source -> say so, never improvise).
        assert "no tengo una fuente en el almacén" in lowered
        assert "improvisar" in lowered
        # Current-question query rule (never an inherited topic).
        assert "nunca un tema heredado de un turno anterior" in lowered
        # No-citations OWNER DECISION (2026-10-14): grounding, not citing.
        assert "citas con autor y año" in lowered
        assert "(autor, año)" not in lowered
        # And the version moved with the behaviour change (semver).
        major, minor, _ = PROMPT_VERSION.split(".")
        assert (major, minor) >= ("1", "4")


class TestRuleSurvivesThePoisonWindow:
    @pytest.fixture
    async def session_factory(self) -> AsyncGenerator[async_sessionmaker[Any], None]:
        try:
            engine: AsyncEngine = await create_test_engine()
        except Exception:
            pytest.skip("Postgres unreachable; pipeline tests require the compose Postgres")
        from app.db.models import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
        await engine.dispose()

    @pytest.fixture
    def session_repository(self) -> Any:
        from app.agent.session_store import DbSessionRepository

        return DbSessionRepository(_test_database_url())

    async def test_second_turn_on_the_same_question_still_delivers_the_re_search_rule(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The live-finding shape: turn 1 answers the question from the
        model's own memory (the wrong "220 - edad" answer); turn 2 asks
        the SAME question again — by then the conversation memory holds
        the earlier reply, exactly the window that taught the model to
        stop searching. The system prompt delivered on turn 2 must still
        carry the re-search rule (pinned; NOT behavioural proof of model
        compliance — only the real model can show that)."""
        question = "¿cómo calculo mi frecuencia cardíaca máxima?"

        # Turn 1: the scripted model answers from memory — the live
        # failure's wrong reply now sits in the conversation memory.
        await process_inbound_message(
            make_message("SMasr00000000000000000000000001", question),
            settings=make_settings(),
            session_factory=session_factory,
            model=FakeModel(["Tu frecuencia cardíaca máxima es 220 menos tu edad."]),
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        # Turn 2: same question again.
        second = FakeModel(["respuesta."])
        await process_inbound_message(
            make_message("SMasr00000000000000000000000002", question),
            settings=make_settings(),
            session_factory=session_factory,
            model=second,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        # The poison is really in the window the second invocation sees.
        assert len(second.invocations) == 1
        window_text = " ".join(
            block.get("text", "")
            for message in second.invocations[0]["messages"]
            for block in message.get("content", [])
        )
        assert "220 menos tu edad" in window_text, (
            "the earlier (wrong) reply must be in the conversation memory "
            "for this test to exercise the poison window"
        )

        # The system prompt delivered on the NEW turn still carries the
        # re-search rule.
        prompt = _collapse(second.invocations[0]["system_prompt"]).lower()
        assert "nunca se arrastra" in prompt
        assert "vuelve a llamar a search_evidence" in prompt
        assert "aunque ya hayas buscado" in prompt
