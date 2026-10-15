"""EV-1 RED tests: the front-matter loader for the evidence library (§8).

Contract pinned here, per the ODD task and the OWNER DECISION recorded in
``odd/tasks/evidence-store.md`` (two source kinds):

- The library holds TWO kinds of source, each a curated markdown file in
  the OWNER'S OWN WORDS (never copied full text, §8):

  - ``kind: paper`` — ``evidence/papers/<slug>.md``, the brief's schema:
    ``title``, ``authors``, ``year``, ``journal``, ``doi``, ``topics``,
    ``key_findings``, ``limitations``;
  - ``kind: guide`` — ``evidence/guides/<slug>.md``, a curated summary of
    a TRAINING DOCUMENT (e.g. the owner's "Pedalea con Ciencia" guide):
    the same common fields, but ``journal``/``doi`` are NOT required (a
    training document has no DOI) and must not be demanded of it.

- Every file declares its ``kind`` in the front matter; an unknown kind
  is a clear, file-specific error.
- Validation fails CLEARLY and SPECIFICALLY on a missing or malformed
  required field: the error names the file and the field.
- The loader walks BOTH directories.
- The citation string works for both kinds and stays TRUTHFUL: a paper
  is cited as ``(Author, Year)`` (§8); a guide is a training document,
  not peer-reviewed literature, so its citation says so
  (``(Author, Year, training guide)``) — a guide must never be citable
  as if it were a study.
- Papers keep the exact path the brief defines: ``evidence/papers/``;
  guides live in the sibling ``evidence/guides/`` — never mixed.

Tests are OFFLINE: every fixture file is written to ``tmp_path`` — the
real ``evidence/`` library stays owner-curated (EV-7) and untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evidence.loader import (
    KIND_GUIDE,
    KIND_PAPER,
    EvidenceFileError,
    load_sources,
    parse_source_file,
)

PAPER_FRONT_MATTER = """\
kind: paper
title: What is sweet-spot training?
authors:
  - Stephen Seiler
year: 2010
journal: International Journal of Sports Physiology and Performance
doi: 10.1080/17461391.2010.1
topics:
  - intensity distribution
key_findings:
  - Elite endurance training is mostly low intensity.
limitations:
  - Observational, causality not established.
"""

GUIDE_FRONT_MATTER = """\
kind: guide
title: Pedalea con Ciencia
authors:
  - Alberto (owner)
year: 2024
topics:
  - cycling training
key_findings:
  - Zone 2 volume builds the aerobic base.
limitations:
  - Curated summary of a training guide, not a study.
"""


def write_source(
    tmp_path: Path, *, directory: str, slug: str, front_matter: str, body: str = "Body text."
) -> Path:
    """One curated source file under tmp evidence/<directory>/<slug>.md."""
    target = tmp_path / directory
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{slug}.md"
    path.write_text(
        f"---\n{front_matter}---\n\n{body}\n", encoding="utf-8"
    )
    return path


def paper_path(tmp_path: Path, front_matter: str = PAPER_FRONT_MATTER) -> Path:
    return write_source(tmp_path, directory="papers", slug="seiler-2010", front_matter=front_matter)


def guide_path(tmp_path: Path, front_matter: str = GUIDE_FRONT_MATTER) -> Path:
    return write_source(
        tmp_path, directory="guides", slug="pedalea-con-ciencia", front_matter=front_matter
    )


# ---------------------------------------------------------------------------
# kind: paper — the brief's §8 schema
# ---------------------------------------------------------------------------


class TestPaperParsing:
    def test_parses_every_required_field(self, tmp_path: Path) -> None:
        source = parse_source_file(paper_path(tmp_path))

        assert source.kind == KIND_PAPER
        assert source.slug == "seiler-2010"
        assert source.title == "What is sweet-spot training?"
        assert source.authors == ("Stephen Seiler",)
        assert source.year == 2010
        assert source.journal == "International Journal of Sports Physiology and Performance"
        assert source.doi == "10.1080/17461391.2010.1"
        assert source.topics == ("intensity distribution",)
        assert source.key_findings == ("Elite endurance training is mostly low intensity.",)
        assert source.limitations == ("Observational, causality not established.",)
        assert source.body == "Body text."

    def test_paper_citation_is_author_year(self, tmp_path: Path) -> None:
        source = parse_source_file(paper_path(tmp_path))

        assert source.citation == "(Stephen Seiler, 2010)"

    def test_paper_requires_journal(self, tmp_path: Path) -> None:
        path = paper_path(
            tmp_path,
            PAPER_FRONT_MATTER.replace(
                "journal: International Journal of Sports Physiology and Performance\n",
                "",
            ),
        )

        with pytest.raises(EvidenceFileError, match=r"journal.*seiler-2010"):
            parse_source_file(path)

    def test_paper_requires_doi(self, tmp_path: Path) -> None:
        path = paper_path(
            tmp_path, PAPER_FRONT_MATTER.replace("doi: 10.1080/17461391.2010.1\n", "")
        )

        with pytest.raises(EvidenceFileError, match=r"doi.*seiler-2010"):
            parse_source_file(path)


# ---------------------------------------------------------------------------
# kind: guide — the owner decision's second source kind
# ---------------------------------------------------------------------------


class TestGuideParsing:
    def test_parses_a_guide_without_journal_or_doi(self, tmp_path: Path) -> None:
        source = parse_source_file(guide_path(tmp_path))

        assert source.kind == KIND_GUIDE
        assert source.slug == "pedalea-con-ciencia"
        assert source.title == "Pedalea con Ciencia"
        assert source.journal is None
        assert source.doi is None
        assert source.body == "Body text."

    def test_guide_citation_stays_truthful_not_a_study(self, tmp_path: Path) -> None:
        source = parse_source_file(guide_path(tmp_path))

        # A guide is a training document, never citable as peer-reviewed
        # literature: its citation string says so explicitly.
        assert source.citation == "(Alberto (owner), 2024, training guide)"
        assert "training guide" in source.citation
        assert source.citation != source.citation.replace("training guide", "study")

    def test_guide_still_requires_its_common_fields(self, tmp_path: Path) -> None:
        path = guide_path(
            tmp_path, GUIDE_FRONT_MATTER.replace("year: 2024\n", "")
        )

        with pytest.raises(EvidenceFileError, match=r"pedalea-con-ciencia.*'year'"):
            parse_source_file(path)


# ---------------------------------------------------------------------------
# Clear, specific failures
# ---------------------------------------------------------------------------


class TestMalformedFiles:
    def test_missing_kind_is_a_named_error(self, tmp_path: Path) -> None:
        path = paper_path(tmp_path, PAPER_FRONT_MATTER.replace("kind: paper\n", ""))

        with pytest.raises(EvidenceFileError, match=r"kind.*seiler-2010"):
            parse_source_file(path)

    def test_unknown_kind_names_the_file_and_accepted_kinds(self, tmp_path: Path) -> None:
        path = paper_path(
            tmp_path, PAPER_FRONT_MATTER.replace("kind: paper", "kind: blog")
        )

        with pytest.raises(EvidenceFileError, match=r"seiler-2010.*unknown kind 'blog'"):
            parse_source_file(path)

    def test_missing_title_is_a_named_error(self, tmp_path: Path) -> None:
        path = paper_path(
            tmp_path,
            PAPER_FRONT_MATTER.replace("title: What is sweet-spot training?\n", ""),
        )

        with pytest.raises(EvidenceFileError, match=r"title.*seiler-2010"):
            parse_source_file(path)

    def test_non_integer_year_is_a_named_error(self, tmp_path: Path) -> None:
        path = paper_path(tmp_path, PAPER_FRONT_MATTER.replace("year: 2010", "year: recent"))

        with pytest.raises(EvidenceFileError, match=r"year.*seiler-2010"):
            parse_source_file(path)

    def test_scalar_authors_is_rejected_authors_are_a_list(self, tmp_path: Path) -> None:
        path = paper_path(
            tmp_path,
            PAPER_FRONT_MATTER.replace(
                "authors:\n  - Stephen Seiler\n", "authors: Stephen Seiler\n"
            ),
        )

        with pytest.raises(EvidenceFileError, match=r"authors.*seiler-2010"):
            parse_source_file(path)

    def test_file_without_front_matter_fails_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "papers" / "no-fence.md"
        path.parent.mkdir(parents=True)
        path.write_text("just prose, no front matter\n", encoding="utf-8")

        with pytest.raises(EvidenceFileError, match="no-fence"):
            parse_source_file(path)

    def test_empty_body_fails_clearly(self, tmp_path: Path) -> None:
        path = write_source(
            tmp_path,
            directory="papers",
            slug="empty",
            front_matter=PAPER_FRONT_MATTER,
            body="   ",
        )

        with pytest.raises(EvidenceFileError, match="empty"):
            parse_source_file(path)


# ---------------------------------------------------------------------------
# Walking both directories
# ---------------------------------------------------------------------------


class TestLoadSources:
    def test_walks_papers_and_guides_sorted_deterministically(
        self, tmp_path: Path
    ) -> None:
        write_source(tmp_path, directory="papers", slug="b-paper", front_matter=PAPER_FRONT_MATTER)
        write_source(tmp_path, directory="papers", slug="a-paper", front_matter=PAPER_FRONT_MATTER)
        write_source(tmp_path, directory="guides", slug="a-guide", front_matter=GUIDE_FRONT_MATTER)

        sources = load_sources(tmp_path)

        assert [(s.slug, s.kind) for s in sources] == [
            ("a-guide", KIND_GUIDE),
            ("a-paper", KIND_PAPER),
            ("b-paper", KIND_PAPER),
        ]

    def test_guides_never_land_in_papers_directory(self, tmp_path: Path) -> None:
        # The brief fixes papers at evidence/papers/<slug>.md; a guide filed
        # there is a curation error the loader must not silently accept as a
        # paper (journal/doi would then be demanded) — it loads as whatever
        # its own kind declares, but the walk must not MIX the directories.
        write_source(tmp_path, directory="guides", slug="a-guide", front_matter=GUIDE_FRONT_MATTER)
        papers_only = load_sources(tmp_path / "papers")
        assert papers_only == []

        sources = load_sources(tmp_path)
        assert [s.slug for s in sources] == ["a-guide"]

    def test_ignores_non_markdown_files(self, tmp_path: Path) -> None:
        (tmp_path / "papers").mkdir(parents=True)
        (tmp_path / "papers" / "notes.txt").write_text("not a paper", encoding="utf-8")

        assert load_sources(tmp_path) == []

    def test_empty_library_is_an_empty_list_not_an_error(self, tmp_path: Path) -> None:
        assert load_sources(tmp_path) == []
