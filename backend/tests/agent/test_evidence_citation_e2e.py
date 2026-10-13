"""EV-10 RED tests: the citation rule end-to-end through the REAL pipeline.

Runs :func:`app.agent.pipeline.process_inbound_message` — the real Strands
loop with the real tool registry, the fake model scripting the turns and
the fake embedding adapter doing every embedding — so the two §12.9
acceptance behaviours are verified as the athlete experiences them:

(a) **Cited answer**: with a synthetic TEST-ONLY paper ingested into the
    dedicated test database (via the REAL ingest pipeline: loader →
    chunker → fake embeddings → pgvector), a scientific question produces
    a reply carrying that source's ``(Author, Year)`` citation — and the
    ``search_evidence`` tool REALLY was called (its result reached the
    model before the final answer and an audit row was persisted).
(b) **Empty store**: with an EMPTY store, the reply states explicitly
    that it has no source in the store and contains NO invented
    ``(Author, Year)``.

The synthetic fixture is test-only data written into ``tmp_path``; the
real ``evidence/`` library is never touched and its content stays the
owner's (EV-7). Everything is offline and deterministic: the fake model
drives the SDK loop and the fake embedding adapter embeds both the
fixture chunk and the query. The settings seam pins
``embedding_provider="fake"`` so ``search_evidence`` can never reach for
the configured production provider.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from app.evidence.embeddings import FakeEmbedding
from app.evidence.ingest import ingest_evidence
from tests import dbsupport

pytestmark = pytest.mark.anyio

SID_CITED = "SMevevs000000000000000000000001"
SID_EMPTY = "SMevevs000000000000000000000002"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"

PAPER_CITATION = "(Stephen Seiler, 2010)"
PAPER_DOI = "10.1000/synthetic-test-fixture"

# Invented-citation detector: any "(Something, YYYY)" the reply was NOT
# handed by a tool is exactly the failure §12.9 forbids.
_INVENTED_CITATION = re.compile(r"\([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ .'-]{0,60}, \d{4}\)")

# Synthetic TEST-ONLY paper (tmp_path fixture; EV-7 stays owner-curated).
PAPER_MD = f"""\
---
kind: paper
title: Synthetic polarized-training study (test fixture)
authors:
  - Stephen Seiler
year: 2010
journal: Journal of Test Fixtures
doi: {PAPER_DOI}
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

Athletes distribute sessions roughly eighty-twenty between low and
high intensity across the season, and the distribution is remarkably
stable between athletes even when the total volume differs widely.
"""


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


async def _tool_results_seen_by_model(model: FakeModel) -> list[dict[str, Any]]:
    """The parsed tool results the loop handed back to the model."""
    results: list[dict[str, Any]] = []
    for invocation in model.invocations:
        for message in invocation["messages"]:
            for block in message["content"]:
                if "toolResult" in block:
                    results.append(json.loads(block["toolResult"]["content"][0]["text"]))
    return results


async def _tool_call_rows(factory: async_sessionmaker[Any]) -> list[MessageLogRow]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(MessageLogRow).where(MessageLogRow.direction == "tool_call")
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
    """Fresh-schema factory on the DEDICATED test database, wired into the
    tool seams together with the fake-embedding settings override."""
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


class TestCitedAnswerEndToEnd:
    async def test_scientific_question_gets_a_cited_answer_and_the_tool_really_ran(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """(a) A scientific question against a seeded store: the scripted
        agent calls ``search_evidence``, SEES the result (citation AND
        DOI travel in it), and the delivered reply carries the source's
        ``(Author, Year)`` citation."""
        cited_reply = (
            "Para el reparto de intensidades, la evidencia apunta a un "
            "modelo polarizado: la mayoría del volumen en intensidad baja "
            "y una pequeña parte en intensidad alta "
            f"{PAPER_CITATION}. El DOI de la fuente está disponible si lo "
            "pides."
        )
        model = FakeModel(
            [
                {
                    "name": "search_evidence",
                    "input": {"query": "training volume and intensity for cyclists"},
                },
                cited_reply,
            ]
        )
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {
                "MessageSid": SID_CITED,
                "From": FROM,
                "To": TO,
                "Body": "¿cómo debo repartir mis intensidades?",
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        # The prompt the model ACTUALLY received pins the final-form rule
        # (whitespace collapsed: the rule wraps across lines).
        system_prompt = re.sub(
            r"\s+", " ", model.invocations[0]["system_prompt"] or ""
        ).lower()
        assert "search_evidence" in system_prompt
        assert "guía de entrenamiento" in system_prompt

        # The tool REALLY was called and its result reached the model.
        seen = await _tool_results_seen_by_model(model)
        assert seen, "search_evidence must have run inside the loop"
        evidence = seen[0]
        assert evidence["status"] == "ok"
        assert evidence["results"][0]["citation"] == PAPER_CITATION
        assert evidence["results"][0]["doi"] == PAPER_DOI

        # ...and was audited as a real tool call.
        audited = await _tool_call_rows(session_factory)
        assert any(row.tool_name == "search_evidence" for row in audited)

        # The delivered reply carries the source's citation.
        reply = twilio.messages.calls[0]["body"]
        assert PAPER_CITATION in reply

    async def test_the_tool_output_reaches_the_model_before_the_final_answer(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        seeded_store: None,
    ) -> None:
        """Ordering proof: the final answer is produced only AFTER the
        search_evidence result was in the loop's message history."""
        model = FakeModel(
            [
                {"name": "search_evidence", "input": {"query": "polarized training"}},
                f"Citado: {PAPER_CITATION}.",
            ]
        )
        await process_inbound_message(
            {"MessageSid": SID_CITED, "From": FROM, "To": TO, "Body": "¿qué dice la evidencia?"},
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=FakeTwilioClient(),
            session_repository=session_repository,
        )

        assert len(model.invocations) == 2
        assert model.invocations[1]["messages"], "the second invocation must replay history"
        history = json.dumps(model.invocations[1]["messages"])
        assert PAPER_CITATION in history


class TestEmptyStoreEndToEnd:
    async def test_unsupported_question_gets_explicit_no_source_and_no_invented_citation(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """(b) EMPTY store: the tool reports insufficient_data, the model
        SEES it, and the delivered reply states explicitly that there is
        no source in the store — with NO invented (Author, Year)."""
        no_source_reply = (
            "No tengo una fuente en el almacén de evidencia que respalde "
            "esa afirmación, así que no te la doy como ciencia: el "
            "propietario aún no ha añadido fuentes a la biblioteca. "
            "Puedo ayudarte con lo que digan tus herramientas de carga y "
            "readiness."
        )
        model = FakeModel(
            [
                {
                    "name": "search_evidence",
                    "input": {"query": "quantum chromodynamics gluon fields"},
                },
                no_source_reply,
            ]
        )
        twilio = FakeTwilioClient()

        await process_inbound_message(
            {
                "MessageSid": SID_EMPTY,
                "From": FROM,
                "To": TO,
                "Body": "¿la gluonodinámica cuántica aplica al ciclismo?",
            },
            settings=pipeline_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        # The tool ran against the EMPTY store and said so explicitly.
        seen = await _tool_results_seen_by_model(model)
        assert seen, "search_evidence must have run inside the loop"
        evidence = seen[0]
        assert evidence["status"] == "insufficient_data"
        assert "empty" in evidence["detail"].lower()
        assert evidence["results"] == []

        # ...and was audited as a real tool call.
        audited = await _tool_call_rows(session_factory)
        assert any(row.tool_name == "search_evidence" for row in audited)

        # The reply states the no-source answer explicitly...
        reply = twilio.messages.calls[0]["body"]
        assert "no tengo una fuente" in reply.lower()
        # ...and invents NO (Author, Year) citation.
        assert _INVENTED_CITATION.search(reply) is None, (
            f"reply must not contain an invented citation: {reply!r}"
        )
