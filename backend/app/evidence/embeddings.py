"""Provider-agnostic embedding adapter (Feature 9, brief §6/§8).

§6's adapter principle, applied to the evidence store exactly as
:mod:`app.agent.model_factory` applies it to the chat model: ONE adapter
object is chosen purely by configuration (``settings.embedding_provider``):

- ``"openai"``: the real OpenAI embeddings adapter built from
  ``settings.openai_api_key`` / ``settings.embedding_model_id``.
  Constructing it wires a local client only — no request is made at
  construction time; the first provider call happens during ingestion or
  retrieval in production.
- ``"fake"``: the deterministic :class:`FakeEmbedding`, which hashes
  tokens into the store's vector space with zero network and zero
  credentials — the provider every automated test uses (no test may open
  a network connection). Same text → same vector; shared vocabulary →
  closer vectors, which is all the offline retrieval tests need.
- anything else fails loudly (``ValueError``): a typo in the provider
  configuration must never silently fall back to a real provider.

Embeddings contain PAPER CONTENT ONLY (§14): never athlete data, never
prompts, never anything derived from Strava or Intervals.icu. Provider
keys come from environment variables only (§14).

The store's vector dimension is fixed at :data:`EMBEDDING_DIMENSION`
(text-embedding-3-small's 1536) and shared by the fake, the column type
(``evidence_chunk.embedding vector(1536)``) and the retrieval query — one
constant so the three can never drift.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

from app.core.settings import Settings

EMBEDDING_DIMENSION = 1536

PROVIDER_OPENAI = "openai"
PROVIDER_FAKE = "fake"

_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingAdapter(Protocol):
    """The thin adapter surface the store needs (§6)."""

    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts; one unit-norm vector per input text."""
        ...


class OpenAIEmbedding:
    """The real provider: OpenAI embeddings via the official client.

    Construction wires a local client only — no request is made until
    :meth:`embed` runs in production (never in tests).
    """

    def __init__(self, *, api_key: str, model_id: str) -> None:
        self.model_id = model_id
        self.dimension = EMBEDDING_DIMENSION
        # Imported lazily: app.db.models imports this module for the shared
        # EMBEDDING_DIMENSION constant, and the DB layer must not pull the
        # whole OpenAI SDK onto every import path (§6 layering).
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts with the configured model (network call; production)."""
        response = self._client.embeddings.create(
            model=self.model_id,
            input=texts,
            dimensions=self.dimension,
        )
        return [item.embedding for item in response.data]


class FakeEmbedding:
    """Deterministic, offline embedding stand-in for tests.

    Each token is md5-hashed to one index in the store's vector space; a
    text's vector is the L2-normalized sum of its token contributions
    (md5, not ``hash()``, so the result never depends on PYTHONHASHSEED).
    Identical texts embed identically; texts sharing vocabulary embed
    closer than texts sharing none.
    """

    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0
        norm = sum(component**2 for component in vector) ** 0.5
        if norm == 0.0:
            return vector
        return [component / norm for component in vector]


def create_embedding(settings: Settings) -> EmbeddingAdapter:
    """Build the ONE embedding adapter the configuration names (§6).

    Raises:
        ValueError: ``settings.embedding_provider`` names no known provider.
    """
    if settings.embedding_provider == PROVIDER_OPENAI:
        return OpenAIEmbedding(
            api_key=settings.openai_api_key,
            model_id=settings.embedding_model_id,
        )
    if settings.embedding_provider == PROVIDER_FAKE:
        return FakeEmbedding()
    raise ValueError(
        f"unknown embedding_provider {settings.embedding_provider!r}: "
        f"expected {PROVIDER_OPENAI!r} or {PROVIDER_FAKE!r}"
    )
