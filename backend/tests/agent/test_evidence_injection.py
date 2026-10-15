"""Deterministic evidence-injection backstop (Feature 9, post live-finding
2026-10-14, third strike).

**Why this exists (live evidence, 2026-10-14).** The conversation memory was
wiped, ``PROMPT_VERSION`` 1.4.0 was verified inside the running container,
the store held 10 chunks of the owner's guide, and retrieval demonstrably
returned the guide's chunk at similarity 0.597 for the exact question — and
the real model still answered "FC max = 220 - edad" having called NO tool.
Prompt rules have failed three times for this behaviour (the citation rule,
the re-search-every-time rule, the tools-outrank-your-memory rule), exactly
as the medical-disclosure rule failed before its deterministic layer. The
conclusion: stop asking, guarantee.

The guarantee, pinned here end-to-end through the REAL pipeline (the real
Strands loop, the FakeModel recording what it ACTUALLY receives, the fake
embedding adapter — offline, dedicated test database):

1. **Per-turn injection**: for every inbound turn, BEFORE the model is
   invoked, the top-k chunks for the owner's CURRENT message are retrieved
   (the SAME retrieval + embedding adapters ``search_evidence`` uses — no
   second query path, no new SQL) and the material is injected into THAT
   turn's system prompt, unmistakably labelled as material from the
   owner's library returned for THIS question, per-turn, never carried
   forward. The block rides the DELIVERED prompt (what the loop hands the
   model), not a template.
2. **The material reaches the model before the final answer**: a turn whose
   script never calls a tool still gets the store's material in the prompt
   of the very invocation that produces the final answer.
3. **Fail-open**: if retrieval raises (embedding adapter, DB session,
   anything), the turn runs EXACTLY as it does today — a reply still goes
   out and is audited. Injection is never a failure path.
4. **Inject NOTHING when nothing qualifies**: empty store, nothing above
   the similarity floor — not even an empty heading; the delivered prompt
   is byte-identical to today's.
5. **Never worded as a citation** (OWNER DECISION 2026-10-14): the block
   carries the material and an explicit "never cite it" rule — no
   ``(Autor, Año)``, no DOI, no bibliographic wording.
6. The existing prompt rules stay (the injection does not replace them),
   and ``PROMPT_VERSION`` is bumped per the prompt file's own rule.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.context import build_athlete_context
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.agent.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from app.core.settings import Settings
from app.db.models import MessageLogRow
from app.evidence.embeddings import FakeEmbedding
from app.evidence.ingest import ingest_evidence
from tests import dbsupport

pytestmark = pytest.mark.anyio

SID_SEEDED = "SMevevi000000000000000000000001"
SID_EMPTY = "SMevevi000000000000000000000002"
SID_FLOOR = "SMevevi000000000000000000000003"
SID_RAISED = "SMevevi000000000000000000000004"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"

# Words the injected block must anchor on (label + per-turn contract +
# never-carried-forward + grounding rule).
LABEL_MARKER = "biblioteca del propietario"
THIS_QUESTION_MARKER = "esta pregunta"
PER_TURN_MARKER = "este turno"
NEVER_CARRIED_MARKER = "nunca se arrastra"
NEVER_CITED_MARKER = "nunca lo cites"
NO_SOURCE_MARKER = "no tienes una fuente en el almacén"

# Invented-citation detector: any "(Something, YYYY)" anywhere in the
# injected delta is exactly what the OWNER DECISION forbids.
_INVENTED_CITATION = re.compile(r"\([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ .'-]{0,60}, \d{4}\)")

PAPER_MD = """\
---
kind: paper
title: Synthetic polarized-training study (test fixture)
authors:
  - Stephen Seiler
year: 2010
journal: Journal of Test Fixtures
doi: 10.1000/synthetic-test-fixture
topics:
  - intensity distribution
key_findings:
  - Elite endurance training is mostly low intensity.
limitations:
  - Synthetic test fixture, not a real study.
---

Polarized training splits the weekly volume into easy low-intensity
sessions and a small number of hard high-intensity sessions.

Sweet-spot work sits just below threshold and raises aerobic power.
"""

# Shares vocabulary with the fixture chunk (polarized, training, intensity,
# volume) so the fake embeddings score it above the 0.1 retrieval floor.
SEEDED_BODY = (
    "Should I use polarized training with mostly low intensity volume?"
)
# Shares NO vocabulary with the fixture chunk: below the floor.
DISJOINT_BODY = "quantum chromodynamics gluon fields"


class FakeTwilioMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, **kwargs: str) -> Any:
        self.calls.append(kwargs)
        return {"sid": "SMout"}


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeTwilioMessages()


def pipeline_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        embedding_provider="fake",
    )


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")


async def _base_prompt(
    session_factory: async_sessionmaker[Any], settings: Settings
) -> str:
    """Today's prompt: SYSTEM_PROMPT + per-turn athlete context, exactly."""
    return SYSTEM_PROMPT + "\n\n" + await build_athlete_context(
        session_factory, settings
    )


async def _outbound_rows(factory: async_sessionmaker[Any]) -> list[MessageLogRow]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(MessageLogRow).where(MessageLogRow.direction == "outbound")
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@pytest.fixture
async def session_factory(
    db_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    factory: async_sessionmaker[Any] = async_sessionmaker(db_engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(pipeline_settings())
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


@pytest.fixture
def session_repository() -> Any:
    from app.agent.session_store import DbSessionRepository

    return DbSessionRepository(dbsupport.test_database_url())


@pytest.fixture
async def seeded_store(
    session_factory: async_sessionmaker[Any], tmp_path: Path
) -> None:
    """Ingest ONE synthetic test-fixture paper through the REAL pipeline
    (loader → chunker → fake embeddings → pgvector) into the dedicated
    test database. tmp_path only — the owner's evidence/ is never touched."""
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "seiler-2010-synthetic.md").write_text(PAPER_MD, encoding="utf-8")
    async with session_factory() as session:
        await ingest_evidence(
            session, settings=object(), root=tmp_path, adapter=FakeEmbedding()
        )
        await session.commit()


class TestDeliveredPromptCarriesTheMaterial:
    async def test_seeded_turn_delivers_material_and_label(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """A turn whose script NEVER calls a tool: the store's material for
        the owner's CURRENT message is in the DELIVERED system prompt,
        labelled as coming from the owner's library for THIS question,
        per-turn, never carried forward."""
        model = FakeModel(["Con la evidencia disponible: reparte el volumen "
                           "en intensidad baja mayoritariamente."])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {
                "MessageSid": SID_SEEDED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        prompt = _collapse(model.invocations[0]["system_prompt"] or "").lower()
        # The fixture material itself rides the delivered prompt...
        assert "polarized training splits the weekly volume" in prompt
        # ...under its unmistakable label: owner's library, THIS question,
        # this turn, never carried forward.
        assert LABEL_MARKER in prompt
        assert THIS_QUESTION_MARKER in prompt
        assert PER_TURN_MARKER in prompt
        assert NEVER_CARRIED_MARKER in prompt

    async def test_material_reaches_the_model_before_the_final_answer(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """The material is in the prompt of the VERY invocation that
        produces the final answer — before any tool round-trip (the script
        has no tool call at all) — not appended after the fact."""
        model = FakeModel(["Respuesta final sin llamar a ninguna herramienta."])

        await process_inbound_message(
            {
                "MessageSid": SID_SEEDED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        assert len(model.invocations) == 1, (
            "the final answer is the FIRST invocation's output: the material "
            "must already be in that invocation's prompt"
        )
        first_prompt = _collapse(model.invocations[0]["system_prompt"] or "").lower()
        assert "polarized training splits the weekly volume" in first_prompt
        assert LABEL_MARKER in first_prompt

    async def test_the_grounding_rule_rides_the_block(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """The injected block instructs: use the material ONLY if its
        content actually supports the claim; ignore it otherwise; the
        material is grounding, never printed; and when it does not support
        the claim (or nothing was found) say explicitly that there is no
        source instead of improvising."""
        model = FakeModel(["ok"])

        await process_inbound_message(
            {
                "MessageSid": SID_SEEDED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        prompt = _collapse(model.invocations[0]["system_prompt"] or "").lower()
        assert NEVER_CITED_MARKER in prompt
        assert NO_SOURCE_MARKER in prompt
        # The material is grounding data, never printed as a reference.
        assert "nunca lo cites" in prompt or "never cite" in prompt

    async def test_existing_prompt_rules_are_kept(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """The injection does not REPLACE the standing prompt rules: the
        evidence rule (search_evidence) and the medical rule stay intact on
        a turn that also carries injected material."""
        model = FakeModel(["ok"])

        await process_inbound_message(
            {
                "MessageSid": SID_SEEDED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        prompt = _collapse(model.invocations[0]["system_prompt"] or "")
        assert "search_evidence" in prompt
        assert "profesional médico" in prompt.lower()
        assert "no diagnostiques" in prompt.lower()


class TestInjectNothingWhenNothingQualifies:
    async def test_empty_store_injects_nothing_prompt_otherwise_unchanged(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """Empty store: NOTHING is injected — not even an empty heading;
        the delivered prompt is byte-identical to today's."""
        model = FakeModel(["ok"])

        await process_inbound_message(
            {
                "MessageSid": SID_EMPTY,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        expected = await _base_prompt(session_factory, pipeline_settings())
        assert model.invocations[0]["system_prompt"] == expected

    async def test_nothing_above_the_floor_injects_nothing(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """A populated store whose chunks are all below the similarity
        floor for this question: nothing is injected, prompt unchanged."""
        model = FakeModel(["ok"])

        await process_inbound_message(
            {
                "MessageSid": SID_FLOOR,
                "From": FROM,
                "To": TO,
                "Body": DISJOINT_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        expected = await _base_prompt(session_factory, pipeline_settings())
        assert model.invocations[0]["system_prompt"] == expected


class TestFailOpen:
    async def test_embedding_failure_does_not_break_the_turn(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The embedding adapter raising must never break the turn: the
        reply still goes out, is audited, and nothing is injected."""

        def _boom(settings: Settings) -> Any:
            raise RuntimeError("embedding provider down")

        monkeypatch.setattr("app.evidence.injection.create_embedding", _boom)
        final_reply = "Tu carga de la semana está controlada."
        model = FakeModel([final_reply])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {
                "MessageSid": SID_RAISED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        # The turn ran exactly as it does today: the reply went out...
        assert len(twilio.messages.calls) == 1
        assert twilio.messages.calls[0]["body"] == final_reply
        # ...and was audited.
        rows = await _outbound_rows(session_factory)
        assert len(rows) == 1
        assert rows[0].body == final_reply
        # Nothing was injected.
        expected = await _base_prompt(session_factory, pipeline_settings())
        assert model.invocations[0]["system_prompt"] == expected

    async def test_retrieval_failure_does_not_break_the_turn(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The retrieval query itself raising must never break the turn."""
        from app.evidence.retrieval import RetrievalResult

        async def _boom(
            session: Any,
            query_embedding: list[float],
            *,
            k: int,
            min_similarity: float = 0.1,
        ) -> RetrievalResult:
            raise RuntimeError("pgvector unreachable")

        monkeypatch.setattr("app.evidence.injection.retrieve_chunks", _boom)
        final_reply = "Tu carga de la semana está controlada."
        model = FakeModel([final_reply])
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {
                "MessageSid": SID_RAISED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        assert len(twilio.messages.calls) == 1
        assert twilio.messages.calls[0]["body"] == final_reply
        rows = await _outbound_rows(session_factory)
        assert len(rows) == 1
        expected = await _base_prompt(session_factory, pipeline_settings())
        assert model.invocations[0]["system_prompt"] == expected


class TestNeverWordedAsACitation:
    async def test_block_carries_no_citation_wording(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """OWNER DECISION (2026-10-14): the injected block is grounding,
        never a citation — no (Autor, Año), no DOI, no bibliographic
        reference, and it explicitly instructs the model never to cite it."""
        model = FakeModel(["ok"])

        await process_inbound_message(
            {
                "MessageSid": SID_SEEDED,
                "From": FROM,
                "To": TO,
                "Body": SEEDED_BODY,
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        delivered = model.invocations[0]["system_prompt"] or ""
        base = await _base_prompt(session_factory, pipeline_settings())
        delta = delivered[len(base):]  # only the injected part
        assert delta.strip(), "the seeded turn must inject a block"
        assert _INVENTED_CITATION.search(delta) is None, (
            f"the injected block must never look like a citation: {delta!r}"
        )
        assert "doi" not in delta.lower()
        # And the block states the no-citation rule itself.
        assert NEVER_CITED_MARKER in _collapse(delta).lower()


class TestPromptVersionBumped:
    def test_prompt_version_bumped_for_the_injection(self) -> None:
        """The prompt file's own rule: bump on any behaviour-changing
        change to the delivered prompt. The injection changes it."""
        parts = PROMPT_VERSION.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts), (
            "PROMPT_VERSION stays a semver string"
        )
        assert PROMPT_VERSION > "1.4.0", (
            "the evidence injection is a behaviour change: PROMPT_VERSION "
            "must move past 1.4.0"
        )
