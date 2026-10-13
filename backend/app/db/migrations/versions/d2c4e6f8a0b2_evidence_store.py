"""evidence_source + evidence_chunk: the evidence store (Feature 9, §6/§8)

Adds the two tables of the evidence store the ``search_evidence`` tool
retrieves from:

- ``evidence_source``: one curated source per unique ``slug``. The OWNER
  DECISION (odd/tasks/evidence-store.md) gives the library two kinds,
  declared by each file's own ``kind`` front-matter field: ``paper``
  (``evidence/papers/<slug>.md``, the brief's §8 schema) and ``guide``
  (``evidence/guides/<slug>.md``, a curated summary of a TRAINING
  DOCUMENT, whose ``journal``/``doi`` are structurally absent/NULL).
  The citation string is decided ONCE at load time — papers as
  ``(Author, Year)`` (§8, DOI on request), guides as
  ``(Author, Year, training guide)`` so a training document can never be
  cited as a peer-reviewed study.
- ``evidence_chunk`` (§6 data model: "paper id, chunk text, embedding,
  citation string"): one embedded chunk per (source, chunk_index), with a
  ``vector`` column at the store's single embedding dimension
  (``app.evidence.embeddings.EMBEDDING_DIMENSION`` = 1536,
  text-embedding-3-small). Embeddings contain SOURCE CONTENT only (§14):
  never athlete data, never prompts, never anything derived from
  Strava/Intervals.icu (§5.3).

Idempotency anchors (§6 convention): ``uq_evidence_source_slug`` and
``uq_evidence_chunk_source_index`` — a re-ingest updates rows in place,
never duplicates them.

The ``vector`` extension comes from the compose image
(``pgvector/pgvector:pg16``, Feature 1); ``create extension if not
exists`` keeps this migration re-runnable. No ANN index on purpose: the
owner-curated library is small (tens of files), so exact cosine distance
over a seq scan is both simpler and exact; add an ivfflat/hnsw index only
if the library grows into the thousands.

NEW revision on top of the applied head ``b8d2e4f6a9c1`` (migration
hygiene: never edit an already-applied migration in place).

Revision ID: d2c4e6f8a0b2
Revises: b8d2e4f6a9c1
Create Date: 2026-10-13 19:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.evidence.embeddings import EMBEDDING_DIMENSION

# revision identifiers, used by Alembic.
revision: str = "d2c4e6f8a0b2"
down_revision: str | None = "b8d2e4f6a9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("create extension if not exists vector")

    op.create_table(
        "evidence_source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            comment=(
                "'paper' (evidence/papers/<slug>.md, brief §8 schema) or "
                "'guide' (evidence/guides/<slug>.md, a curated summary of "
                "a training document; OWNER DECISION)."
            ),
        ),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "authors",
            sa.JSON(),
            nullable=False,
            comment="Author list as curated in the file's front matter.",
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column(
            "journal",
            sa.Text(),
            nullable=True,
            comment="Papers only; NULL for guides (a training document has no journal).",
        ),
        sa.Column(
            "doi",
            sa.Text(),
            nullable=True,
            comment="Papers only; NULL for guides (a training document has no DOI).",
        ),
        sa.Column(
            "citation",
            sa.Text(),
            nullable=False,
            comment=(
                "Decided once at load time: '(Author, Year)' for papers "
                "(§8), '(Author, Year, training guide)' for guides so a "
                "guide is never citable as a study."
            ),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_evidence_source_slug"),
    )
    op.create_table(
        "evidence_chunk",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "embedding",
            Vector(EMBEDDING_DIMENSION),
            nullable=False,
            comment=(
                "pgvector embedding of the chunk text at the store's single "
                "dimension; source content only (§14)."
            ),
        ),
        sa.Column(
            "citation",
            sa.Text(),
            nullable=False,
            comment="Copied from the source row so retrieval can cite without a join.",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["evidence_source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "chunk_index", name="uq_evidence_chunk_source_index"
        ),
    )
    op.create_index(
        "ix_evidence_chunk_source_id", "evidence_chunk", ["source_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_chunk_source_id", table_name="evidence_chunk")
    op.drop_table("evidence_chunk")
    op.drop_table("evidence_source")
    # The extension is NOT dropped: other objects in the database may use
    # the vector type, and dropping it is destructive beyond this schema.
