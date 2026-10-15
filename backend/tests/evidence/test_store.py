"""EV-4 RED tests: pgvector storage for the evidence store (§6, §8).

Contract pinned here:

- ``evidence_source``: one row per curated source (papers AND guides —
  the owner decision's two kinds), keyed by the unique ``slug``; the
  citation string lives on the row so it is decided ONCE at load time.
- ``evidence_chunk`` (§6 data model): paper/source id, chunk text,
  embedding (``vector``, the store's fixed dimension) and the citation
  string, unique per ``(source_id, chunk_index)``.
- The repository upserts are IDEMPOTENT (the ingest-schema convention):
  re-ingesting the same slug/chunk index updates the row, never
  duplicates it.
- DB-backed tests run on the dedicated test database (conftest) and
  never on the developer's database; they stay offline (fake embeddings).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvidenceChunkRow, EvidenceSourceRow
from app.db.repository import (
    delete_stale_evidence_chunks,
    upsert_evidence_chunk,
    upsert_evidence_source,
)

pytestmark = pytest.mark.anyio

PAPER_CITATION = "(Stephen Seiler, 2010)"
GUIDE_CITATION = "(Owner, 2024, training guide)"


def make_embedding() -> list[float]:
    return [0.5] + [0.0] * 1535


@pytest.fixture
async def paper_source(db_session: AsyncSession) -> EvidenceSourceRow:
    return await upsert_evidence_source(
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


@pytest.fixture
async def guide_source(db_session: AsyncSession) -> EvidenceSourceRow:
    return await upsert_evidence_source(
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


class TestEvidenceSourceStorage:
    async def test_paper_and_guide_sources_persist(
        self, db_session: AsyncSession, paper_source: EvidenceSourceRow,
        guide_source: EvidenceSourceRow,
    ) -> None:
        rows = (
            (await db_session.execute(select(EvidenceSourceRow).order_by(EvidenceSourceRow.slug)))
            .scalars()
            .all()
        )
        assert [(row.slug, row.kind, row.citation) for row in rows] == [
            ("pedalea-con-ciencia", "guide", GUIDE_CITATION),
            ("seiler-2010", "paper", PAPER_CITATION),
        ]

    async def test_source_upsert_is_idempotent_by_slug(
        self, db_session: AsyncSession, paper_source: EvidenceSourceRow
    ) -> None:
        updated = await upsert_evidence_source(
            db_session,
            kind="paper",
            slug="seiler-2010",
            title="What is sweet-spot training? (revised)",
            authors=["Stephen Seiler"],
            year=2010,
            journal="IJSPP",
            doi="10.1080/1",
            citation=PAPER_CITATION,
        )

        count = (
            await db_session.execute(select(func.count()).select_from(EvidenceSourceRow))
        ).scalar_one()
        assert count == 1
        assert updated.id == paper_source.id
        # Read the PERSISTED row (the session's identity map holds the
        # pre-update instance; flush-only upserts do not expire it).
        await db_session.refresh(updated)
        assert updated.title == "What is sweet-spot training? (revised)"


class TestEvidenceChunkStorage:
    async def test_chunk_row_holds_source_id_text_embedding_citation(
        self, db_session: AsyncSession, paper_source: EvidenceSourceRow
    ) -> None:
        row = await upsert_evidence_chunk(
            db_session,
            source_id=paper_source.id,
            chunk_index=0,
            text="Elite endurance training is mostly low intensity.",
            embedding=make_embedding(),
            citation=PAPER_CITATION,
        )

        stored = await db_session.get(EvidenceChunkRow, row.id)
        assert stored is not None
        assert stored.source_id == paper_source.id
        assert stored.chunk_index == 0
        assert stored.text == "Elite endurance training is mostly low intensity."
        assert stored.citation == PAPER_CITATION
        assert len(stored.embedding) == 1536
        assert stored.embedding[0] == pytest.approx(0.5)

    async def test_chunk_upsert_is_idempotent_by_source_and_index(
        self, db_session: AsyncSession, paper_source: EvidenceSourceRow
    ) -> None:
        first = await upsert_evidence_chunk(
            db_session,
            source_id=paper_source.id,
            chunk_index=0,
            text="Original text.",
            embedding=make_embedding(),
            citation=PAPER_CITATION,
        )
        second = await upsert_evidence_chunk(
            db_session,
            source_id=paper_source.id,
            chunk_index=0,
            text="Re-ingested text.",
            embedding=make_embedding(),
            citation=PAPER_CITATION,
        )

        count = (
            await db_session.execute(select(func.count()).select_from(EvidenceChunkRow))
        ).scalar_one()
        assert count == 1
        assert second.id == first.id
        await db_session.refresh(second)
        assert second.text == "Re-ingested text."

    async def test_stale_chunk_indexes_are_deleted(
        self, db_session: AsyncSession, paper_source: EvidenceSourceRow
    ) -> None:
        for index in range(3):
            await upsert_evidence_chunk(
                db_session,
                source_id=paper_source.id,
                chunk_index=index,
                text=f"chunk {index}",
                embedding=make_embedding(),
                citation=PAPER_CITATION,
            )

        # A re-ingest that now produces only 2 chunks must remove chunk 2:
        # derived data never survives its own source shrink.
        await delete_stale_evidence_chunks(db_session, source_id=paper_source.id, from_index=2)

        remaining = (
            (
                await db_session.execute(
                    select(EvidenceChunkRow.chunk_index)
                    .where(EvidenceChunkRow.source_id == paper_source.id)
                    .order_by(EvidenceChunkRow.chunk_index)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == [0, 1]
