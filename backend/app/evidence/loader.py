"""Front-matter loader for the evidence library (Feature 9, brief §8).

**File format** (chosen for honesty and simplicity): each source is one
markdown file whose metadata lives in a YAML front-matter block delimited
by ``---`` fences — the de-facto standard for markdown with metadata, so
the owner curates files in any markdown editor and the loader stays a
thin, well-defined parse. YAML needs one small dependency (``pyyaml``),
declared explicitly; the alternative hand-rolled ``key: value`` format
would have grown a bespoke list-syntax grammar for ``authors``/
``key_findings`` — more code and more ways to be silently wrong.

**Two source kinds** (OWNER DECISION, odd/tasks/evidence-store.md): the
library holds more than papers. Every file declares its ``kind``:

- ``kind: paper`` — ``evidence/papers/<slug>.md`` (the exact path the
  brief fixes). Required fields: ``title``, ``authors``, ``year``,
  ``journal``, ``doi``, ``topics``, ``key_findings``, ``limitations``.
- ``kind: guide`` — ``evidence/guides/<slug>.md``, a curated summary of
  a TRAINING DOCUMENT (e.g. the owner's "Pedalea con Ciencia" guide).
  Required fields: the same minus ``journal``/``doi`` — a training
  document has no DOI, and demanding one would be wrong. The original
  document stays an original file; only the owner's own-words summary is
  ingested, never copied text (§8).

**Per-kind citation rule** (§8, made truthful for both kinds): papers are
cited ``(Author, Year)`` — the brief's format, DOI available on request
(it is front matter). A guide is a training document, NOT peer-reviewed
literature, so its citation says so: ``(Author, Year, training guide)``.
The ``kind`` travels with every chunk and retrieval result so the agent
can phrase the citation honestly and never present a guide as a study.

**Validation is loud and specific** (§8): a missing or malformed required
field raises :class:`EvidenceFileError` naming the file and the field —
a typo in the owner's library must never become a silently half-ingested
source.

Paths: the loader walks ``<root>/papers/*.md`` and ``<root>/guides/*.md``
(deterministically sorted); the directories are never mixed — a guide
belongs to ``evidence/guides/``, never inside ``evidence/papers/``. The
default root is the repository's ``evidence/`` directory. No source file
exists yet (EV-7 is owner-curated), so an empty library is the CURRENT
true state and loads as an empty list, not an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

KIND_PAPER = "paper"
KIND_GUIDE = "guide"
KINDS = (KIND_PAPER, KIND_GUIDE)

_GUIDE_CITATION_MARKER = "training guide"

# Repository layout: backend/app/evidence/loader.py -> repo root/evidence.
DEFAULT_EVIDENCE_ROOT = Path(__file__).resolve().parents[3] / "evidence"

_FRONT_MATTER_FENCE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Common required fields (both kinds); papers additionally need journal+doi.
_COMMON_REQUIRED = ("title", "authors", "year", "topics", "key_findings", "limitations")
_PAPER_ONLY_REQUIRED = ("journal", "doi")
_LIST_FIELDS = ("authors", "topics", "key_findings", "limitations")


class EvidenceFileError(ValueError):
    """A source file is missing or malformed where it must not be.

    The message names the file and the field so the owner can fix the
    curation error in one step; the loader never guesses a value.
    """


@dataclass(frozen=True)
class EvidenceSource:
    """One curated source: its metadata, its citation string, its body."""

    slug: str
    kind: str
    title: str
    authors: tuple[str, ...]
    year: int
    journal: str | None
    doi: str | None
    topics: tuple[str, ...]
    key_findings: tuple[str, ...]
    limitations: tuple[str, ...]
    citation: str
    body: str
    path: Path


def _error(path: Path, message: str) -> EvidenceFileError:
    return EvidenceFileError(f"{path}: {message}")


def _require_str(meta: dict[str, object], field: str, path: Path) -> str:
    value = meta.get(field)
    if value is None:
        raise _error(path, f"missing required front-matter field {field!r}")
    if not isinstance(value, str) or not value.strip():
        raise _error(path, f"field {field!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _require_str_list(meta: dict[str, object], field: str, path: Path) -> tuple[str, ...]:
    value = meta.get(field)
    if value is None:
        raise _error(path, f"missing required front-matter field {field!r}")
    if not isinstance(value, list) or not value:
        raise _error(
            path,
            f"field {field!r} must be a non-empty YAML list "
            f"(one '- item' per line), got {value!r}",
        )
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _error(path, f"field {field!r} has a non-string/empty entry: {item!r}")
        items.append(item.strip())
    return tuple(items)


def _require_int(meta: dict[str, object], field: str, path: Path) -> int:
    value = meta.get(field)
    if value is None:
        raise _error(path, f"missing required front-matter field {field!r}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, f"field {field!r} must be an integer year, got {value!r}")
    return value


def citation_for(kind: str, first_author: str, year: int) -> str:
    """The truthful citation string for a source kind (§8, owner decision).

    Papers: ``(Author, Year)`` — DOI available on request. Guides: the
    citation names itself a training guide, so a guide can never be
    presented as peer-reviewed literature.
    """
    if kind == KIND_GUIDE:
        return f"({first_author}, {year}, {_GUIDE_CITATION_MARKER})"
    return f"({first_author}, {year})"


def parse_source_file(path: Path) -> EvidenceSource:
    """Parse and validate one curated source file.

    Raises:
        EvidenceFileError: missing ``---`` fences, a missing/malformed
            required field for the file's declared kind, an unknown
            ``kind``, or an empty body.
    """
    text = path.read_text(encoding="utf-8")
    fence = _FRONT_MATTER_FENCE.match(text)
    if fence is None:
        raise _error(
            path,
            "no YAML front matter found: the file must start with a "
            "'---' fence, then 'key: value' metadata, then a closing "
            "'---' fence before the body",
        )
    try:
        meta = yaml.safe_load(fence.group(1))
    except yaml.YAMLError as exc:
        raise _error(path, f"front matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise _error(path, f"front matter must be a mapping of fields, got {meta!r}")

    kind = meta.get("kind")
    if kind is None:
        raise _error(path, "missing required front-matter field 'kind'")
    if kind not in KINDS:
        raise _error(
            path,
            f"unknown kind {kind!r}: expected one of {list(KINDS)} "
            f"('paper' files live in evidence/papers/, 'guide' files in "
            "evidence/guides/)",
        )

    title = _require_str(meta, "title", path)
    authors = _require_str_list(meta, "authors", path)
    year = _require_int(meta, "year", path)
    for field in _COMMON_REQUIRED:
        if meta.get(field) is None:
            raise _error(path, f"missing required front-matter field {field!r}")
    topics = _require_str_list(meta, "topics", path)
    key_findings = _require_str_list(meta, "key_findings", path)
    limitations = _require_str_list(meta, "limitations", path)

    journal: str | None = None
    doi: str | None = None
    if kind == KIND_PAPER:
        journal = _require_str(meta, "journal", path)
        doi = _require_str(meta, "doi", path)

    body = text[fence.end() :].strip()
    if not body:
        raise _error(
            path, "empty body: the file has front matter but no summary text"
        )

    return EvidenceSource(
        slug=path.stem,
        kind=kind,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        topics=topics,
        key_findings=key_findings,
        limitations=limitations,
        citation=citation_for(kind, authors[0], year),
        body=body,
        path=path,
    )


def load_sources(root: Path | None = None) -> list[EvidenceSource]:
    """Load every curated source under ``root``, papers and guides both.

    Walks ``<root>/papers/*.md`` and ``<root>/guides/*.md`` separately and
    returns them sorted by ``(kind, slug)`` — a deterministic order so a
    re-ingest is stable. Non-markdown files are ignored; an empty library
    (the state today: EV-7 is owner-curated, no files exist yet) returns
    ``[]`` rather than raising. A malformed file raises
    :class:`EvidenceFileError` naming file and field.
    """
    base = DEFAULT_EVIDENCE_ROOT if root is None else root
    sources: list[EvidenceSource] = []
    for directory in ("guides", "papers"):
        subdir = base / directory
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.glob("*.md")):
            sources.append(parse_source_file(path))
    sources.sort(key=lambda source: (source.kind, source.slug))
    return sources
