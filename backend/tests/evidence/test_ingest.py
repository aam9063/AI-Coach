"""EV-4 (pipeline) RED tests: the ingestion pipeline (§8 "Ingest").

Contract pinned here:

- ``app.evidence.ingest.ingest_evidence(session, settings, root)`` runs
  the WHOLE pipeline offline against the deterministic fake embedding:
  load sources (papers AND guides) -> chunk with citations -> embed ->
  idempotent upsert into ``evidence_source``/``evidence_chunk``.
- Idempotent: ingesting the same library twice does not duplicate
  anything; re-ingesting an UPDATED file replaces its chunks and removes
  chunk indexes the file no longer has (derived data never outlives its
  source text).
- The report states what was ingested so the CLI output is auditable.
- Tests use ``tmp_path`` fixture files (EV-7 stays owner-curated; the
  real ``evidence/`` library is never touched and stays empty).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import EvidenceChunkRow, EvidenceSourceRow
from app.evidence.ingest import ingest_evidence

pytestmark = pytest.mark.anyio

PAPER_MD = """\
---
kind: paper
title: What is sweet-spot training?
authors:
  - Stephen Seiler
year: 2010
journal: IJSPP
doi: 10.1080/1
topics:
  - intensity distribution
key_findings:
  - Elite endurance training is mostly low intensity.
limitations:
  - Observational, causality not established.
---

Polarized training splits the weekly volume into easy low-intensity
sessions and a small number of hard high-intensity sessions.

Sweet-spot work sits just below threshold and raises aerobic power.

The threshold-heavy model was rare in the observed elite cohorts.

Athletes distribute sessions roughly eighty-twenty between low and
high intensity across the season, and the distribution is remarkably
stable between athletes even when the total volume differs widely.

Causality remains unestablished because the designs are observational
and the athletes self-select their training intensity distribution,
so selection effects cannot be ruled out.

Future work should follow larger cohorts over multiple seasons with
objective intensity control rather than self-reported training logs,
and should report the underlying session-intensity data openly so the
distribution model can be reproduced and falsified by others.
"""

GUIDE_MD = """\
---
kind: guide
title: Pedalea con Ciencia
authors:
  - Owner
year: 2024
topics:
  - cycling training
key_findings:
  - Zone 2 volume builds the aerobic base.
limitations:
  - Curated summary of a training guide, not a study.
---

Zone 2 riding builds the aerobic base with low strain.
"""


def make_settings() -> Settings:
    return Settings(_env_file=None, embedding_provider="fake")  # type: ignore[call-arg]


def write_library(tmp_path: Path) -> None:
    (tmp_path / "papers").mkdir(parents=True)
    (tmp_path / "papers" / "seiler-2010.md").write_text(PAPER_MD, encoding="utf-8")
    (tmp_path / "guides").mkdir(parents=True)
    (tmp_path / "guides" / "pedalea-con-ciencia.md").write_text(GUIDE_MD, encoding="utf-8")


@pytest.fixture
async def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def session(db_engine: Any) -> AsyncGenerator[AsyncSession, None]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


class TestIngestPipeline:
    async def test_ingests_papers_and_guides_with_citations(
        self, session: AsyncSession, settings: Settings, tmp_path: Path
    ) -> None:
        write_library(tmp_path)

        reports = await ingest_evidence(session, settings=settings, root=tmp_path)

        assert {report.slug for report in reports} == {
            "seiler-2010",
            "pedalea-con-ciencia",
        }
        paper = (
            await session.execute(
                select(EvidenceSourceRow).where(EvidenceSourceRow.slug == "seiler-2010")
            )
        ).scalar_one()
        assert paper.kind == "paper"
        assert paper.citation == "(Stephen Seiler, 2010)"
        guide = (
            await session.execute(
                select(EvidenceSourceRow).where(
                    EvidenceSourceRow.slug == "pedalea-con-ciencia"
                )
            )
        ).scalar_one()
        assert guide.kind == "guide"
        assert "training guide" in guide.citation

        chunks = (
            (
                await session.execute(
                    select(EvidenceChunkRow).where(
                        EvidenceChunkRow.source_id == paper.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert chunks
        assert all(chunk.citation == paper.citation for chunk in chunks)
        assert all(len(chunk.embedding) == 1536 for chunk in chunks)

    async def test_reingesting_the_same_library_is_idempotent(
        self, session: AsyncSession, settings: Settings, tmp_path: Path
    ) -> None:
        write_library(tmp_path)
        await ingest_evidence(session, settings=settings, root=tmp_path)
        first_counts = (
            await session.execute(
                select(func.count()).select_from(EvidenceChunkRow)
            )
        ).scalar_one()

        await ingest_evidence(session, settings=settings, root=tmp_path)

        second_counts = (
            await session.execute(
                select(func.count()).select_from(EvidenceChunkRow)
            )
        ).scalar_one()
        assert second_counts == first_counts

    async def test_shrunk_source_loses_its_stale_chunks(
        self, session: AsyncSession, settings: Settings, tmp_path: Path
    ) -> None:
        write_library(tmp_path)
        await ingest_evidence(session, settings=settings, root=tmp_path)
        paper = (
            await session.execute(
                select(EvidenceSourceRow).where(EvidenceSourceRow.slug == "seiler-2010")
            )
        ).scalar_one()
        before = (
            await session.execute(
                select(func.count())
                .select_from(EvidenceChunkRow)
                .where(EvidenceChunkRow.source_id == paper.id)
            )
        ).scalar_one()
        assert before >= 2

        # The owner trims the paper to one paragraph: chunk 1 must vanish.
        (tmp_path / "papers" / "seiler-2010.md").write_text(
            PAPER_MD.split("\n\n")[0] + "\n\nPolarized training splits the volume.",
            encoding="utf-8",
        )
        await ingest_evidence(session, settings=settings, root=tmp_path)

        remaining = (
            await session.execute(
                select(func.count())
                .select_from(EvidenceChunkRow)
                .where(EvidenceChunkRow.source_id == paper.id)
            )
        ).scalar_one()
        assert remaining == 1
