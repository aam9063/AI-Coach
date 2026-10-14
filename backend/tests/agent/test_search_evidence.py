"""EV-6 RED tests: the ``search_evidence`` tool (§9.3, Feature 9).

Contract pinned here, matching the WA-6 tool contract exactly:

- **Typed result** (§9.3): ``status`` ok/insufficient_data/error,
  ``engine_version`` + ``computed_at`` (settings/now — retrieval reads no
  persisted engine row), and ``coverage`` (k requested vs chunks
  returned, and the similarity floor), built from the typed Pydantic
  :class:`app.evidence.retrieval.RetrievalResult`.
- **Explicit empty store** (§8, §9.3): the store may LEGITIMATELY be
  empty today (the owner has not curated the library yet) — the tool
  returns ``status="insufficient_data"`` whose detail says the agent
  must answer that it has NO SOURCE in the store instead of
  improvising. This is the CURRENT true state, not an error.
- **No relevant results** is also ``insufficient_data`` (never ``ok``
  with weak hits, never zeros).
- **Argument errors** (§9.3 precedent): a blank query or an out-of-range
  ``k`` is an ``error`` naming the accepted values — never missing data.
- **Offline**: the embeddings provider is the deterministic fake
  (pinned via the settings seam); no test may open a network connection.
- ``tool_list()`` grows by exactly this one tool (the registry stays
  EXPLICIT — never the SDK's directory loader).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.core.settings import Settings
from app.db.repository import upsert_evidence_chunk, upsert_evidence_source
from app.evidence.embeddings import FakeEmbedding

pytestmark = pytest.mark.anyio

PAPER_CITATION = "(Stephen Seiler, 2010)"
POLARIZED_CHUNK = (
    "Polarized training splits the weekly volume into easy low-intensity "
    "sessions and a small number of hard high-intensity sessions."
)

fake = FakeEmbedding()


def tool_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        engine_version="test-engine-1.0",
        embedding_provider="fake",
    )


@pytest.fixture
async def tool_db(
    db_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    factory: async_sessionmaker[Any] = async_sessionmaker(db_engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(tool_settings())
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


@pytest.fixture
async def seeded_store(tool_db: async_sessionmaker[Any]) -> None:
    async with tool_db() as session:
        paper = await upsert_evidence_source(
            session,
            kind="paper",
            slug="seiler-2010",
            title="What is sweet-spot training?",
            authors=["Stephen Seiler"],
            year=2010,
            journal="IJSPP",
            doi="10.1080/1",
            citation=PAPER_CITATION,
        )
        await upsert_evidence_chunk(
            session,
            source_id=paper.id,
            chunk_index=0,
            text=POLARIZED_CHUNK,
            embedding=fake.embed([POLARIZED_CHUNK])[0],
            citation=PAPER_CITATION,
        )
        await session.commit()


class TestSearchEvidenceOk:
    async def test_returns_chunks_with_citations_and_provenance(
        self, seeded_store: None
    ) -> None:
        result = await tools.search_evidence(
            "training volume and intensity for cyclists", k=4
        )

        assert result["status"] == "ok"
        assert result["engine_version"] == "test-engine-1.0"
        assert result["computed_at"]
        assert result["results"]
        first = result["results"][0]
        assert first["citation"] == PAPER_CITATION
        assert first["kind"] == "paper"
        assert first["source_slug"] == "seiler-2010"
        assert 0.0 < first["similarity"] <= 1.0

    async def test_coverage_states_k_returned_and_floor(self, seeded_store: None) -> None:
        result = await tools.search_evidence(
            "training volume and intensity for cyclists", k=4
        )

        assert result["coverage"]["k_requested"] == 4
        assert result["coverage"]["chunks_returned"] == len(result["results"])
        assert result["coverage"]["min_similarity"] > 0

    async def test_paper_results_carry_the_doi(self, seeded_store: None) -> None:
        """EV-9 RED: the tool result itself carries the DOI, so the
        "DOI available on request" promise is keepable from the result
        the model sees — never from memory."""
        result = await tools.search_evidence(
            "training volume and intensity for cyclists", k=4
        )

        assert result["status"] == "ok"
        assert result["results"][0]["doi"] == "10.1080/1"


class TestSearchEvidenceExplicitEmpty:
    async def test_empty_store_is_insufficient_data_telling_the_agent_to_say_so(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.search_evidence("polarized training", k=4)

        assert result["status"] == "insufficient_data"
        detail = result["detail"].lower()
        assert "empty" in detail
        # The model-facing instruction: no source in the store -> say so,
        # never improvise.
        assert "no source in the store" in detail
        assert "improvis" in detail
        assert result["results"] == []
        assert result["engine_version"] == "test-engine-1.0"
        assert result["computed_at"]

    async def test_no_relevant_detail_reports_the_real_store_count_not_k(
        self, seeded_store: None, tool_db: async_sessionmaker[Any]
    ) -> None:
        """DEFECT-1 RED (live finding): the tool relays the detail the
        model reads — its count must be the store's REAL chunk count,
        never ``len(results)`` (capped at ``k``). The store here holds 2
        chunks; with k=1 the old code reported "1 chunks"."""
        async with tool_db() as session:
            guide = await upsert_evidence_source(
                session,
                kind="guide",
                slug="pedalea-con-ciencia",
                title="Pedalea con Ciencia",
                authors=["Owner"],
                year=2024,
                journal=None,
                doi=None,
                citation="(Owner, 2024, training guide)",
            )
            await upsert_evidence_chunk(
                session,
                source_id=guide.id,
                chunk_index=0,
                text="Threshold work raises aerobic power.",
                embedding=fake.embed(["Threshold work raises aerobic power."])[0],
                citation="(Owner, 2024, training guide)",
            )
            await session.commit()

        result = await tools.search_evidence("quantum chromodynamics gluon fields", k=1)

        assert result["status"] == "insufficient_data"
        assert "2 chunks" in result["detail"]

    async def test_no_relevant_detail_never_claims_an_empty_store(
        self, seeded_store: None
    ) -> None:
        """DEFECT-1 (live finding, 2026-10-14): a populated store must
        never be described as empty — the model answered from memory after
        being told that."""
        result = await tools.search_evidence("quantum chromodynamics gluon fields", k=4)

        assert result["status"] == "insufficient_data"
        assert "empty" not in result["detail"].lower()

    async def test_no_relevant_results_is_insufficient_data_not_ok(
        self, seeded_store: None
    ) -> None:
        result = await tools.search_evidence("quantum chromodynamics gluon fields", k=4)

        assert result["status"] == "insufficient_data"
        assert "similar" in result["detail"].lower()
        assert result["results"] == []


class TestSearchEvidenceArgumentErrors:
    async def test_blank_query_is_an_error_naming_the_problem(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.search_evidence("   ", k=4)

        assert result["status"] == "error"
        assert "query" in result["detail"].lower()
        assert result["engine_version"] == "test-engine-1.0"
        assert result["computed_at"]

    async def test_out_of_range_k_is_an_error_naming_the_range(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.search_evidence("polarized training", k=0)

        assert result["status"] == "error"
        assert "1" in result["detail"] and "10" in result["detail"]

    async def test_out_of_range_k_high_is_an_error_too(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.search_evidence("polarized training", k=11)

        assert result["status"] == "error"


class TestToolListGrowsByExactlyOne:
    def test_tool_list_contains_search_evidence(self) -> None:
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in tools.tool_list()
        }
        assert "search_evidence" in names
        assert len(tools.tool_list()) == 7

    def test_tool_list_stays_explicit_no_vended_tools(self) -> None:
        for listed in tools.tool_list():
            underlying = getattr(listed, "_tool_func", None) or listed
            assert getattr(underlying, "__module__", "app.agent.tools").startswith(
                "app.agent"
            )


class TestDocstringIsTheModelFacingSchema:
    def test_docstring_says_the_query_is_the_owner_current_question(self) -> None:
        """DEFECT-1b RED (live finding, 2026-10-14): the model reused the
        PREVIOUS turn's topic as the search query. The model-facing schema
        must say the query is the owner's CURRENT question."""
        doc = tools.search_evidence.__doc__ or ""

        assert "CURRENT" in doc
        assert "earlier turn" in doc.lower()

    def test_docstring_says_similarity_is_a_hint_not_a_verdict(self) -> None:
        """DEFECT-2 RED: the model-facing schema must say a chunk counts
        as a source only if its CONTENT supports the claim — similarity is
        a hint, never a verdict of relevance."""
        doc = tools.search_evidence.__doc__ or ""

        assert "hint" in doc.lower()
        assert "support" in doc.lower()

    def test_docstring_states_the_empty_store_instruction(self) -> None:
        doc = tools.search_evidence.__doc__ or ""

        assert "empty" in doc.lower()
        # The store may legitimately be empty today — the docstring must
        # say what the agent does then.
        assert "no source" in doc.lower()
        assert "improvis" in doc.lower()

    def test_docstring_does_not_instruct_printing_citations(self) -> None:
        """OWNER DECISION (2026-10-14) RED: the tool schema must NOT
        instruct the model to print citations — the store grounds the
        answer, and the stored citation strings are internal data."""
        doc = tools.search_evidence.__doc__ or ""

        assert "(author, year)" not in doc.lower()
        assert "training guide" not in doc.lower()
        assert "never print" in doc.lower() or "do not print" in doc.lower()

    def test_docstring_makes_no_doi_promise(self) -> None:
        """OWNER DECISION (2026-10-14) RED: no DOI-on-request promise
        anywhere in the model-facing schema (the ``doi`` field stays
        internal data on the result payload only)."""
        doc = tools.search_evidence.__doc__ or ""

        assert "on request" not in doc.lower()
        assert "doi" not in doc.lower()
