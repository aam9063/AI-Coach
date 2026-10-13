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
    def test_docstring_states_the_empty_store_instruction(self) -> None:
        doc = tools.search_evidence.__doc__ or ""

        assert "empty" in doc.lower()
        # The store may legitimately be empty today — the docstring must
        # say what the agent does then.
        assert "no source" in doc.lower()
        assert "improvis" in doc.lower()

    def test_docstring_documents_both_citation_forms(self) -> None:
        doc = tools.search_evidence.__doc__ or ""

        # Papers: (Author, Year). Guides: a training guide, never a study.
        assert "Author, Year" in doc
        assert "training guide" in doc.lower()

    def test_docstring_says_results_include_the_doi(self) -> None:
        """EV-9 RED: the model-facing schema must say the result carries
        the DOI (papers), so the "DOI available on request" promise in
        the system prompt is grounded in the tool's own output."""
        doc = tools.search_evidence.__doc__ or ""

        assert "doi" in doc.lower()
        assert "on request" in doc.lower()
