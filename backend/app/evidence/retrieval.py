"""Retrieval: top-k chunks by cosine similarity (Feature 9, brief §8).

§8: "Retrieval tool returns chunks with citations." The contract this
module keeps (and the ``search_evidence`` tool relays):

- **Top-k by cosine similarity** over ``evidence_chunk.embedding``,
  most similar first, capped at ``k``.
- **Citations travel with every chunk** (§8) — plus the source's slug and
  ``kind`` (owner decision: the agent must cite a guide as a training
  guide, never as a study).
- **An empty store is an EXPLICIT result**, never an empty list passed
  off as an answer: ``status="empty_store"`` with a ``detail`` naming
  what is missing and how to get it. No paper/guide files exist yet
  (EV-7 is owner-curated), so this is the store's CURRENT true state —
  a legitimate state, not an error.
- **No relevant results is also explicit**: chunks exist but none clears
  the :data:`MIN_SIMILARITY` floor → ``status="no_relevant_results"``,
  never an ``ok`` with silently-weak hits.

:data:`MIN_SIMILARITY` (0.1) is the floor below which a chunk is treated
as NOT supporting a query at all (OWNER CHOICE, no literature source —
sized like the §9.2 guardrails): it must sit comfortably below what even
loosely-related texts score (real embedding models: ~0.3+; the test fake:
~0.18+) while staying clearly above the ~0.0 of disjoint vocabulary, so
the "no relevant results" branch remains reachable and testable offline.
It is a floor against improvising from irrelevant chunks, not a
relevance quality bar.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvidenceChunkRow, EvidenceSourceRow

MIN_SIMILARITY: Final[float] = 0.1

Status = Literal["ok", "empty_store", "no_relevant_results"]

_EMPTY_STORE_DETAIL = (
    "the evidence store is empty: no curated papers or training guides "
    "have been ingested yet. This is a legitimate state (the owner "
    "curates the library by hand) — say that you have no source in the "
    "store for this claim instead of improvising. The owner adds files "
    "under evidence/papers/ and evidence/guides/ and runs "
    "`python -m app.evidence.ingest` to embed them."
)

_NO_RESULTS_DETAIL = (
    "the evidence store has {n_chunks} chunks, but none is similar "
    "enough to the query (cosine similarity floor {floor}): no stored "
    "source supports this claim. Say that you have no source in the "
    "store instead of improvising."
)


class RetrievedChunk(BaseModel):
    """One retrieved chunk with its truthful citation (§8)."""

    source_slug: str
    kind: str
    citation: str
    chunk_index: int
    text: str
    similarity: float = Field(description="cosine similarity, in [-1, 1]")


class RetrievalResult(BaseModel):
    """The typed retrieval outcome — never a bare list (§9.3)."""

    status: Status
    results: list[RetrievedChunk]
    detail: str | None = None
    coverage: dict[str, float | int]


async def retrieve_chunks(
    session: AsyncSession,
    query_embedding: list[float],
    *,
    k: int,
    min_similarity: float = MIN_SIMILARITY,
) -> RetrievalResult:
    """Return the top-``k`` chunks by cosine similarity to the query.

    Raises:
        ValueError: ``k`` is not a positive integer (an argument error,
            never missing data).
    """
    if k < 1:
        raise ValueError(f"k must be a positive integer, got {k}")

    distance = EvidenceChunkRow.embedding.cosine_distance(query_embedding)
    rows = (
        (
            await session.execute(
                select(EvidenceChunkRow, EvidenceSourceRow, distance.label("distance"))
                .join(EvidenceSourceRow, EvidenceChunkRow.source_id == EvidenceSourceRow.id)
                .order_by(distance)
                .limit(k)
            )
        )
        .all()
    )

    coverage: dict[str, float | int] = {
        "k_requested": k,
        "chunks_returned": 0,
        "min_similarity": min_similarity,
    }
    if not rows:
        return RetrievalResult(
            status="empty_store",
            results=[],
            detail=_EMPTY_STORE_DETAIL,
            coverage=coverage,
        )

    results = [
        RetrievedChunk(
            source_slug=source.slug,
            kind=source.kind,
            citation=chunk.citation,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            similarity=1.0 - db_distance,
        )
        for chunk, source, db_distance in rows
    ]
    relevant = [chunk for chunk in results if chunk.similarity >= min_similarity]
    if not relevant:
        return RetrievalResult(
            status="no_relevant_results",
            results=[],
            detail=_NO_RESULTS_DETAIL.format(n_chunks=len(results), floor=min_similarity),
            coverage=coverage,
        )

    coverage["chunks_returned"] = len(relevant)
    return RetrievalResult(status="ok", results=relevant, detail=None, coverage=coverage)
