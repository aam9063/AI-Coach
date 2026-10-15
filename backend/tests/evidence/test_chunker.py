"""EV-3 RED tests: the deterministic chunker (§8).

Contract pinned here:

- Deterministic: the same input always yields the exact same chunks —
  no RNG, no locale, no wall-clock anywhere in the path.
- Sentence-preserving: chunk boundaries never cut a sentence; every
  sentence of the body appears, whole, in at least one chunk (the overlap
  repeats sentences across chunk boundaries on purpose).
- Overlap: a chunk that follows another starts with the tail sentence(s)
  of the previous chunk, so a retrieved chunk carries its own context.
- Citation carrying (§8): EVERY chunk carries its paper's citation string
  verbatim — retrieval returns chunks with citations, never bare text.
- The kind travels with the chunk (owner decision: a guide chunk must be
  citable only as a training guide, never as a study).
"""

from __future__ import annotations

from itertools import pairwise

from app.evidence.chunker import Chunk, chunk_text

CITATION = "(Stephen Seiler, 2010)"
SENTENCES = [
    "Elite endurance athletes train mostly at low intensity.",
    "Polarized training splits volume between easy work and short hard sessions.",
    "The threshold-heavy model was rare in the observed cohorts.",
    "Athletes distribute sessions roughly eighty-twenty between low and high intensity.",
    "Causality remains unestablished because the designs are observational.",
]
BODY = " ".join(SENTENCES)


def sentence_chunks(
    *, citation: str = CITATION, kind: str = "paper", max_chars: int = 120
) -> list[Chunk]:
    return chunk_text(BODY, citation=citation, kind=kind, max_chars=max_chars)


class TestDeterminism:
    def test_same_input_yields_identical_chunks_twice(self) -> None:
        first = sentence_chunks()
        second = sentence_chunks()

        assert first == second

    def test_chunk_indexes_are_dense_from_zero(self) -> None:
        chunks = sentence_chunks()

        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


class TestSentencePreservation:
    def test_no_sentence_is_cut_in_half(self) -> None:
        chunks = sentence_chunks()

        for chunk in chunks:
            for sentence in SENTENCES:
                # A sentence is never partially present: it is either in a
                # chunk whole or not in that chunk at all.
                contained = sentence in chunk.text
                if contained:
                    assert chunk.text.count(sentence) == 1

        covered = [s for s in SENTENCES if any(s in c.text for c in chunks)]
        assert covered == SENTENCES

    def test_a_sentence_longer_than_max_chars_becomes_its_own_chunk(self) -> None:
        long_sentence = "word " * 50 + "end."
        chunks = chunk_text(long_sentence, citation=CITATION, kind="paper", max_chars=40)

        assert len(chunks) == 1
        assert chunks[0].text == long_sentence
        # The boundary rule holds even for the oversized chunk: no new cut
        # is invented mid-sentence just to respect max_chars.


class TestOverlap:
    def test_chunk_n_starts_with_the_tail_of_chunk_n_minus_one(self) -> None:
        chunks = sentence_chunks()

        assert len(chunks) >= 2
        for previous, current in pairwise(chunks):
            assert current.text.startswith(previous.tail_text())
            assert current.index == previous.index + 1


class TestCitationCarrying:
    def test_every_chunk_carries_its_citation_verbatim(self) -> None:
        chunks = sentence_chunks()

        assert chunks
        assert all(chunk.citation == CITATION for chunk in chunks)

    def test_kind_travels_with_the_chunk(self) -> None:
        chunks = sentence_chunks(kind="guide", citation="(Owner, 2024, training guide)")

        assert all(chunk.kind == "guide" for chunk in chunks)
