"""Retrieval: top-k chunks by cosine similarity (Feature 9, brief §8).

§8: "Retrieval tool returns chunks with citations." The contract this
module keeps (and the ``search_evidence`` tool relays):

- **Top-k by cosine similarity** over ``evidence_chunk.embedding``,
  most similar first, capped at ``k``.
- **Citations travel with every chunk** (§8) — plus the source's slug and
  ``kind`` (owner decision: the agent must cite a guide as a training
  guide, never as a study). The paper's ``doi`` also travels with every
  result (EV-9): "DOI available on request" (§8) is a promise the agent
  can only keep if the tool output itself carries the DOI — never one
  recalled from the model's memory. A guide has no DOI, so its results
  carry ``doi=None``, never an invented identifier.
- **An empty store is an EXPLICIT result**, never an empty list passed
  off as an answer: ``status="empty_store"`` with a ``detail`` naming
  what is missing and how to get it. No paper/guide files exist yet
  (EV-7 is owner-curated), so this is the store's CURRENT true state —
  a legitimate state, not an error. That emptiness claim is a CHECKED
  claim, never an inference (live finding, 2026-10-14: the model was
  told the store was empty while it held 10 chunks, believed it had no
  sources at all, answered a scientific question from memory and
  contradicted the ingested guide): the empty wording is emitted only
  after an actual ``COUNT`` proves the store holds zero chunks; if the
  distance query yields no rows while chunks exist, the result is the
  honest per-query "no relevant results" outcome instead — the agent
  must NEVER be told the store is empty while it holds chunks.
- **No relevant results is also explicit**: chunks exist but none clears
  the :data:`MIN_SIMILARITY` floor → ``status="no_relevant_results"``,
  never an ``ok`` with silently-weak hits. Its detail is the per-query
  truth — nothing matches THIS query — never a store-level emptiness
  claim, and the only chunk count it names is the store's REAL chunk
  count (an actual ``COUNT``), never ``len(results)``: that is capped
  at the requested ``k``, so with a 10-chunk store and ``k=3`` the old
  wording told the model the store has 3 chunks — a number the model
  read and repeated.
:data:`MIN_SIMILARITY` (0.1) is a CROSS-DOMAIN floor, not a relevance
bar — it cannot be a relevance bar, and the observed numbers say why.
Measured against this store with the production provider
(text-embedding-3-small, 2026-10-14, six bounded exploratory queries):
questions the guide genuinely answers score 0.60-0.69 (HRmax question
0.597; the guide's own topic 0.689), Spanish cycling questions the
guide does NOT cover score 0.38-0.56 (target power 0.427; on-ride
nutrition 0.562), and clearly disjoint questions score 0.04-0.19
(tax return 0.191; capital of Australia 0.049). Same-domain-irrelevant
nearly touches genuinely-relevant, so NO floor value can decide
relevance. 0.1 is an OWNER CHOICE (no literature source — sized like
the §9.2 guardrails): it rejects only clearly disjoint or
cross-vocabulary queries, keeping the "no relevant results" branch
reachable and testable offline (it stays below the test fake's
related-pair floor ~0.18; tests use the fake, never the network).
Trade-off, stated plainly: too high a floor rejects real matches
(only two genuinely-relevant queries observed so far), too low lets
unsupported same-domain chunks through. The defense against
same-domain-irrelevant chunks is NOT this floor — it is the AGENT-side
rule in the prompt (rule 4): a returned chunk counts as a source ONLY
if its content actually supports the claim; the similarity is a hint,
never a verdict.

"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.db.models import EvidenceChunkRow, EvidenceSourceRow

# CROSS-DOMAIN floor only — see the module docstring for the measured
# evidence and the honest limits of what this value can discriminate
# (it is NOT a relevance bar; the agent-side content rule in the prompt
# is the defense against same-domain-irrelevant chunks).
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
    "the evidence store holds {n_chunks} chunks, but none is similar "
    "enough to this query (cosine similarity floor {floor}): no stored "
    "source supports this claim. Say that you have no source for this "
    "question instead of improvising."
)


def _store_chunk_count_stmt() -> Select[int]:
    """The COUNT statement that proves (or refutes) store-level emptiness."""
    return select(func.count()).select_from(EvidenceChunkRow)


class RetrievedChunk(BaseModel):
    """One retrieved chunk with its truthful citation (§8).

    ``doi`` (EV-9): the source's DOI for papers — the payload behind the
    prompt's "DOI available on request" promise — and ``None`` for a
    guide (a training document has no DOI; absence is explicit, never a
    guess).
    """

    source_slug: str
    kind: str
    citation: str
    doi: str | None = Field(
        default=None,
        description="DOI of the source (papers only; a guide has none)",
    )
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
        # The emptiness claim is a CHECKED claim, never an inference (live
        # finding, 2026-10-14): the distance query yielding no rows is not
        # by itself proof the store is empty, so verify with an actual
        # COUNT first. While chunks exist, the agent must NEVER be told
        # the store is empty — the honest per-query outcome is returned
        # instead, with the store's real chunk count.
        store_count = await session.scalar(_store_chunk_count_stmt()) or 0
        if store_count == 0:
            return RetrievalResult(
                status="empty_store",
                results=[],
                detail=_EMPTY_STORE_DETAIL,
                coverage=coverage,
            )
        return RetrievalResult(
            status="no_relevant_results",
            results=[],
            detail=_NO_RESULTS_DETAIL.format(
                n_chunks=store_count, floor=min_similarity
            ),
            coverage=coverage,
        )

    results = [
        RetrievedChunk(
            source_slug=source.slug,
            kind=source.kind,
            citation=chunk.citation,
            doi=source.doi,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            similarity=1.0 - db_distance,
        )
        for chunk, source, db_distance in rows
    ]
    relevant = [chunk for chunk in results if chunk.similarity >= min_similarity]
    if not relevant:
        # The count the model reads is the store's REAL chunk count (an
        # actual COUNT), never ``len(results)`` — that is capped at the
        # requested ``k`` and told the model a 10-chunk store had 3.
        store_count = await session.scalar(_store_chunk_count_stmt()) or 0
        return RetrievalResult(
            status="no_relevant_results",
            results=[],
            detail=_NO_RESULTS_DETAIL.format(
                n_chunks=store_count, floor=min_similarity
            ),
            coverage=coverage,
        )

    coverage["chunks_returned"] = len(relevant)
    return RetrievalResult(status="ok", results=relevant, detail=None, coverage=coverage)
