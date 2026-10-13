"""The evidence ingestion pipeline (Feature 9, brief §8 "Ingest").

Runs the whole §8 flow over the owner-curated library — load (both kinds:
papers under ``evidence/papers/``, guides under ``evidence/guides/``),
chunk with citations, embed through the configured adapter (§6; the
deterministic fake for tests), idempotently upsert into
``evidence_source``/``evidence_chunk`` — and removes chunk indexes a
shrunk source no longer has (derived data never outlives its source
text, so a stale chunk can never keep being retrieved).

Embeddings contain SOURCE CONTENT ONLY (§14): the pipeline never touches
athlete data, prompts or anything derived from Strava/Intervals.icu.

Usage (the command the ``search_evidence`` insufficient-data detail
names):

    python -m app.evidence.ingest
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.repository import (
    delete_stale_evidence_chunks,
    upsert_evidence_chunk,
    upsert_evidence_source,
)
from app.db.session import create_db_engine, make_session_factory
from app.evidence.chunker import chunk_text
from app.evidence.embeddings import EmbeddingAdapter, create_embedding
from app.evidence.loader import EvidenceSource, load_sources


@dataclass(frozen=True)
class IngestReport:
    """What one source contributed, for auditable CLI output."""

    slug: str
    kind: str
    citation: str
    chunks_stored: int


async def ingest_source(
    session: AsyncSession,
    source: EvidenceSource,
    *,
    adapter: EmbeddingAdapter,
) -> IngestReport:
    """Ingest ONE loaded source: upsert row, chunk, embed, upsert chunks."""
    row = await upsert_evidence_source(
        session,
        kind=source.kind,
        slug=source.slug,
        title=source.title,
        authors=list(source.authors),
        year=source.year,
        journal=source.journal,
        doi=source.doi,
        citation=source.citation,
    )
    chunks = chunk_text(source.body, citation=source.citation, kind=source.kind)
    vectors = adapter.embed([chunk.text for chunk in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        await upsert_evidence_chunk(
            session,
            source_id=row.id,
            chunk_index=chunk.index,
            text=chunk.text,
            embedding=vector,
            citation=source.citation,
        )
    await delete_stale_evidence_chunks(
        session, source_id=row.id, from_index=len(chunks)
    )
    return IngestReport(
        slug=source.slug,
        kind=source.kind,
        citation=source.citation,
        chunks_stored=len(chunks),
    )


async def ingest_evidence(
    session: AsyncSession,
    *,
    settings: object,
    root: Path | None = None,
    adapter: EmbeddingAdapter | None = None,
) -> list[IngestReport]:
    """Ingest every curated source under ``root`` (default: the repo's
    ``evidence/`` library).

    Idempotent: re-ingesting the same library stores nothing twice, and a
    file edited between ingests is fully replaced (its stale chunks
    deleted). Flushes but does NOT commit — callers own the transaction.
    """
    embedding_adapter = adapter if adapter is not None else create_embedding(settings)  # type: ignore[arg-type]
    return [
        await ingest_source(session, source, adapter=embedding_adapter)
        for source in load_sources(root)
    ]


async def main() -> None:
    """CLI entry point: ingest the repository's evidence library."""
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            reports = await ingest_evidence(session, settings=settings)
            await session.commit()
    finally:
        await engine.dispose()
    if not reports:
        print(
            "evidence store: nothing ingested — the library is empty "
            "(evidence/papers/ and evidence/guides/ have no .md files yet)."
        )
        return
    for report in reports:
        print(
            f"evidence store: {report.slug} ({report.kind}) -> "
            f"{report.chunks_stored} chunks, citation {report.citation}"
        )


if __name__ == "__main__":
    asyncio.run(main())
