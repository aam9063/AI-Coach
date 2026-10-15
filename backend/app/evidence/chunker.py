"""Deterministic sentence-preserving chunker (Feature 9, brief §8).

The evidence library is chunked before embedding so retrieval returns
passage-sized pieces a citation can point at. The contract:

- **Deterministic**: pure text in, chunks out — no RNG, no locale, no
  wall clock. The same input yields byte-identical chunks forever, so a
  re-ingest is stable and reviewable.
- **Sentence-preserving**: chunk boundaries fall BETWEEN sentences, never
  inside one; a sentence longer than ``max_chars`` becomes its own
  oversized chunk rather than being cut mid-sentence. A training summary
  cut at an arbitrary character offset would embed a broken claim.
- **Overlap**: each following chunk starts with the tail sentence(s) of
  the previous one (default 1), so a retrieved chunk carries its own
  context even when it lands at a boundary.
- **Citation carrying** (§8): every chunk carries its source's citation
  string verbatim, plus the source's ``kind`` (owner decision: a guide
  chunk must stay citable as a training guide, never as a study).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 800
DEFAULT_OVERLAP_SENTENCES = 1

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class Chunk:
    """One chunk: a whole-sentence span plus its source's citation/kind."""

    index: int
    text: str
    citation: str
    kind: str
    tail: tuple[str, ...]
    """The chunk's last sentence(s) — the next chunk's overlap prefix."""

    def tail_text(self) -> str:
        """The tail sentences joined the same way they sit in ``text``."""
        return " ".join(self.tail)


def split_sentences(text: str) -> list[str]:
    """Split into sentences on ``.!?…`` followed by whitespace, deterministically."""
    return [
        sentence
        for sentence in (s.strip() for s in _SENTENCE_BOUNDARY.split(text))
        if sentence
    ]


def chunk_text(
    text: str,
    *,
    citation: str,
    kind: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
) -> list[Chunk]:
    """Chunk ``text`` into whole-sentence chunks of at most ``max_chars``.

    Greedy sentence packing with a ``overlap_sentences``-deep overlap: the
    first sentence(s) of chunk *n+1* repeat the tail of chunk *n*. Every
    chunk gets at least one NEW sentence (progress is guaranteed even when
    the overlap itself exceeds ``max_chars``), and no sentence is ever cut
    in half. An empty or whitespace-only body yields ``[]``.
    """
    sentences = split_sentences(text)
    chunks: list[Chunk] = []
    overlap: tuple[str, ...] = ()
    position = 0
    while position < len(sentences):
        parts = list(overlap)
        parts.append(sentences[position])
        position += 1
        while position < len(sentences):
            candidate = " ".join([*parts, sentences[position]])
            if len(candidate) > max_chars:
                break
            parts.append(sentences[position])
            position += 1
        tail = tuple(parts[-overlap_sentences:]) if overlap_sentences > 0 else ()
        chunks.append(
            Chunk(
                index=len(chunks),
                text=" ".join(parts),
                citation=citation,
                kind=kind,
                tail=tail,
            )
        )
        overlap = tail
    return chunks
