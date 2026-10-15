"""EV-4 RED tests: the embeddings adapter (§6 adapter principle, §8).

Contract (mirrors ``app.agent.model_factory`` WA-4 exactly, §6 "thin
adapter interface, switchable by configuration"):

- ``app.evidence.embeddings.create_embedding(settings)`` returns ONE
  embedding adapter chosen purely by configuration
  (``settings.embedding_provider``):
  ``"openai"`` -> the real OpenAI embeddings adapter built from the
  settings (constructing it wires a local client only — NO request is
  made, and no test ever calls it: every automated test uses the fake);
  ``"fake"`` -> the deterministic :class:`app.evidence.embeddings.FakeEmbedding`
  (zero network, zero credentials);
  anything else fails loudly (``ValueError``), never a silent fallback.
- The fake is DETERMINISTIC: the same text always embeds to the same
  vector, and texts that share vocabulary embed closer together than
  texts that share none — enough for the offline retrieval tests.
- The adapter reports the store's embedding dimension; the fake's output
  dimension matches it exactly.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.settings import Settings
from app.evidence.embeddings import (
    EMBEDDING_DIMENSION,
    FakeEmbedding,
    OpenAIEmbedding,
    create_embedding,
)


def make_settings(**overrides: Any) -> Settings:
    """Settings isolated from any real .env file (no credentials read)."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestProviderSelection:
    def test_openai_provider_builds_the_openai_adapter_from_settings(self) -> None:
        settings = make_settings(
            embedding_provider="openai",
            embedding_model_id="text-embedding-3-small",
            openai_api_key="sk-test-not-real",
        )
        adapter = create_embedding(settings)

        assert isinstance(adapter, OpenAIEmbedding)
        assert adapter.model_id == "text-embedding-3-small"

    def test_fake_provider_builds_the_deterministic_fake(self) -> None:
        settings = make_settings(embedding_provider="fake")

        assert isinstance(create_embedding(settings), FakeEmbedding)

    def test_unknown_provider_fails_loudly(self) -> None:
        settings = make_settings(embedding_provider="cohere")

        with pytest.raises(ValueError, match="embedding_provider"):
            create_embedding(settings)


class TestFakeEmbedding:
    def test_dimension_matches_the_store_dimension(self) -> None:
        fake = FakeEmbedding()

        assert fake.dimension == EMBEDDING_DIMENSION
        vectors = fake.embed(["any text"])
        assert len(vectors[0]) == EMBEDDING_DIMENSION

    def test_same_text_embeds_identically(self) -> None:
        fake = FakeEmbedding()

        assert fake.embed(["polarized training"]) == fake.embed(["polarized training"])

    def test_shared_vocabulary_is_closer_than_disjoint_vocabulary(self) -> None:
        fake = FakeEmbedding()
        query = fake.embed(["polarized training in endurance sports"])[0]
        related = fake.embed(["Polarized training splits volume into easy and hard sessions."])[0]
        unrelated = fake.embed(["quantum chromodynamics describes gluon fields"])[0]

        def dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b, strict=False))

        assert dot(query, related) > dot(query, unrelated)
        # And the unrelated pair is essentially orthogonal (deterministic
        # separation the retrieval threshold can rely on).
        assert dot(query, unrelated) < 0.3

    def test_embeddings_are_unit_norm(self) -> None:
        fake = FakeEmbedding()

        vector = fake.embed(["low intensity volume"])[0]
        norm = sum(x * x for x in vector) ** 0.5

        assert norm == pytest.approx(1.0)
