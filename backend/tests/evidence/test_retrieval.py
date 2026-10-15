"""EV-5 RED tests: retrieval — top-k by cosine similarity (§8).

Contract pinned here:

- Top-k by COSINE similarity over ``evidence_chunk.embedding``; each
  result carries its chunk text, its citation string (§8) and its
  source's slug AND kind (owner decision: the agent must be able to cite
  a guide as a training guide, never as a study).
- An EMPTY store is an EXPLICIT result (``status="empty_store"`` with a
  ``detail`` naming what is missing and how to get it) — never an empty
  list passed off as an answer. No paper/guide files exist yet, so this
  is the CURRENT true state of the store, not an error.
- When the store has rows but NONE is similar enough (below the
  module's ``MIN_SIMILARITY`` floor), the result is
  ``status="no_relevant_results"`` with a ``detail`` naming the floor —
  also never a bare empty list.
- Results are ordered by similarity, most similar first; ``k`` caps how
  many are returned.
- Tests stay OFFLINE: embeddings come from the deterministic fake.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvidenceChunkRow
from app.db.repository import upsert_evidence_chunk, upsert_evidence_source
from app.evidence.embeddings import FakeEmbedding
from app.evidence.retrieval import MIN_SIMILARITY, RetrievalResult, retrieve_chunks

pytestmark = pytest.mark.anyio

PAPER_CITATION = "(Stephen Seiler, 2010)"
GUIDE_CITATION = "(Owner, 2024, training guide)"

POLARIZED_CHUNK = (
    "Polarized training splits the weekly volume into easy low-intensity "
    "sessions and a small number of hard high-intensity sessions."
)
THRESHOLD_CHUNK = (
    "Sweet-spot and threshold work raises the aerobic power of trained "
    "cyclists without excessive strain."
)

fake = FakeEmbedding()


@pytest.fixture
async def store_with_sources(db_session: AsyncSession) -> AsyncGenerator[dict[str, Any], None]:
    """One paper and one guide, each with one chunk, embedded by the fake."""
    paper = await upsert_evidence_source(
        db_session,
        kind="paper",
        slug="seiler-2010",
        title="What is sweet-spot training?",
        authors=["Stephen Seiler"],
        year=2010,
        journal="IJSPP",
        doi="10.1080/1",
        citation=PAPER_CITATION,
    )
    guide = await upsert_evidence_source(
        db_session,
        kind="guide",
        slug="pedalea-con-ciencia",
        title="Pedalea con Ciencia",
        authors=["Owner"],
        year=2024,
        journal=None,
        doi=None,
        citation=GUIDE_CITATION,
    )
    await upsert_evidence_chunk(
        db_session,
        source_id=paper.id,
        chunk_index=0,
        text=POLARIZED_CHUNK,
        embedding=fake.embed([POLARIZED_CHUNK])[0],
        citation=PAPER_CITATION,
    )
    await upsert_evidence_chunk(
        db_session,
        source_id=guide.id,
        chunk_index=0,
        text=THRESHOLD_CHUNK,
        embedding=fake.embed([THRESHOLD_CHUNK])[0],
        citation=GUIDE_CITATION,
    )
    await db_session.commit()
    yield {"paper": paper, "guide": guide}


async def retrieve(db_session: AsyncSession, query: str, k: int = 4) -> RetrievalResult:
    query_embedding = fake.embed([query])[0]
    return await retrieve_chunks(db_session, query_embedding, k=k)


class TestTopKRetrieval:
    async def test_most_similar_chunk_comes_first_with_its_citation(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        result = await retrieve(db_session, "How should endurance training be polarized?")

        assert result.status == "ok"
        assert result.results
        assert result.results[0].citation == PAPER_CITATION
        assert "polarized" in result.results[0].text.lower()
        similarities = [chunk.similarity for chunk in result.results]
        assert similarities == sorted(similarities, reverse=True)

    async def test_k_caps_the_number_of_results(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        result = await retrieve(db_session, "training for cyclists", k=1)

        assert result.status == "ok"
        assert len(result.results) == 1

    async def test_kind_and_slug_travel_with_each_result(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        # A query that genuinely overlaps both chunks (the fake scores the
        # paper chunk 0.42 and the guide chunk 0.21, both above the floor;
        # the polarized query of the first test overlaps ONLY the paper).
        result = await retrieve(db_session, "training volume and intensity for cyclists")

        kinds = {chunk.kind for chunk in result.results}
        slugs = {chunk.source_slug for chunk in result.results}
        assert kinds == {"paper", "guide"}
        assert slugs == {"seiler-2010", "pedalea-con-ciencia"}

    async def test_a_guide_result_keeps_its_truthful_training_guide_citation(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        result = await retrieve(db_session, "threshold work for trained cyclists")

        guide_results = [c for c in result.results if c.kind == "guide"]
        assert guide_results
        assert all("training guide" in c.citation for c in guide_results)

    async def test_paper_results_carry_the_doi(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        """EV-9 RED: "DOI available on request" is a promise the agent can
        only keep if the tool's result carries the DOI — so retrieval must
        deliver the source's DOI with every paper chunk."""
        result = await retrieve(db_session, "How should endurance training be polarized?")

        paper_results = [c for c in result.results if c.kind == "paper"]
        assert paper_results
        assert all(c.doi == "10.1080/1" for c in paper_results)

    async def test_guide_results_have_no_doi(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        """EV-9 RED: a training document has no DOI — the field is None,
        never an invented identifier."""
        result = await retrieve(db_session, "threshold work for trained cyclists")

        guide_results = [c for c in result.results if c.kind == "guide"]
        assert guide_results
        assert all(c.doi is None for c in guide_results)


class TestExplicitEmptyAndNoRelevance:
    async def test_empty_store_is_an_explicit_status_not_a_bare_list(
        self, db_session: AsyncSession
    ) -> None:
        result = await retrieve(db_session, "anything")

        assert result.status == "empty_store"
        assert result.results == []
        assert result.detail is not None
        assert "empty" in result.detail.lower()
        assert "ingest" in result.detail.lower()

    async def test_disjoint_query_gets_no_relevant_results_not_ok(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        result = await retrieve(db_session, "quantum chromodynamics gluon fields")

        assert result.status == "no_relevant_results"
        assert result.results == []
        assert result.detail is not None
        assert "similar" in result.detail.lower()
        assert str(MIN_SIMILARITY) in result.detail

    async def test_no_relevant_detail_reports_the_store_real_chunk_count(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        """DEFECT-1 RED (live finding): the detail the MODEL reads must
        never show a false number. Observed live: the store held 10 chunks
        and the model was told it has 3 — the old code printed
        ``len(results)``, which is capped at the requested ``k``. The
        count in the detail must be the store's REAL chunk count."""
        real_count = len(
            (await db_session.execute(select(EvidenceChunkRow.id))).all()
        )
        assert real_count == 2  # the fixture's store; k=1 below caps results at 1

        result = await retrieve(
            db_session, "quantum chromodynamics gluon fields", k=1
        )

        assert result.status == "no_relevant_results"
        assert result.detail is not None
        assert f"{real_count} chunks" in result.detail

    async def test_no_relevant_detail_never_claims_the_store_is_empty(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        """DEFECT-1 RED (live finding, 2026-10-14): the model was told the
        store was empty while it held 10 chunks, believed it had no
        sources at all and answered a scientific question from memory.
        The no-relevant-results wording must be the per-query truth —
        nothing matches THIS query — never a store-level emptiness claim."""
        result = await retrieve(db_session, "quantum chromodynamics gluon fields")

        assert result.status == "no_relevant_results"
        assert result.detail is not None
        assert "empty" not in result.detail.lower()
        assert (
            "this query" in result.detail.lower()
            or "this claim" in result.detail.lower()
        )

    async def test_store_never_told_empty_while_it_holds_chunks(
        self,
    ) -> None:
        """DEFECT-1 RED (the live acceptance case): the agent must NEVER
        be told ``empty_store`` while the store holds chunks.

        The distance query returning no rows is not by itself proof the
        store is empty, so the empty wording must be contingent on an
        actual count check. That anomaly is unreachable through the public
        interface with valid data (the distance query returns up to ``k``
        rows of whatever exists), so this test pins the guard at its seam:
        a session whose similarity search yields no rows while a COUNT
        proves chunks exist."""

        class _Result:
            def all(self) -> list[Any]:
                return []

        class _UnreachableChunks:
            async def execute(self, stmt: Any) -> _Result:
                return _Result()

            async def scalar(self, stmt: Any) -> int:
                return 10

        result = await retrieve_chunks(
            cast(AsyncSession, _UnreachableChunks()),
            fake.embed(["any question"])[0],
            k=4,
        )

        assert result.status == "no_relevant_results"
        assert result.detail is not None
        assert "empty" not in result.detail.lower()
        assert "10 chunks" in result.detail

    async def test_min_similarity_floor_is_below_the_fake_related_pair(
        self, store_with_sources: dict[str, Any]
    ) -> None:
        """The floor must sit between the fake's disjoint (0.0) and its
        related pairs (~0.18+) so both branches stay reachable offline."""
        assert 0.0 < MIN_SIMILARITY < 0.18

    async def test_coverage_states_what_backed_the_answer(
        self, db_session: AsyncSession, store_with_sources: dict[str, Any]
    ) -> None:
        result = await retrieve(db_session, "How should endurance training be polarized?", k=2)

        assert result.coverage["k_requested"] == 2
        assert result.coverage["chunks_returned"] == len(result.results)
        assert result.coverage["min_similarity"] == MIN_SIMILARITY
