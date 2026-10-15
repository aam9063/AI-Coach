"""Pure parser for the cycling workout corpus (WLK-2; ODD feature workout-library).

**Provenance.** Parses the owner's reference file
``docs/exercises/EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt`` (1,727 lines,
CRLF endings) into typed, frozen data. The parser is PURE text -> data: no
file I/O, no clock, no network. I/O belongs to the loader and the fixture
generation script (WLK-4/WLK-5), mirroring the engine purity conventions.

**Measured grammar of THIS file** (re-measured by hand with whole-line
matching; see ``tests/agent/test_workout_library.py`` for the accounting
regression that enforces every number):

- Line 1 is a corpus title (ends with ``:``), not a workout.
- 29 sessions: 5 labelled ``E1``..``E5`` — E2 and E4 are free-text
  descriptions (E2's on the line after the header, E4's on the header line
  itself, ``E4:SALIDA LIBRE: ...`` with no space after the label colon) —
  plus 24 unlabelled sessions starting with ``Warm up``. Sessions are
  separated by lines that are exactly ``--``; the file ends with a trailing
  ``--`` which produces no phantom session.
- 361 steps: 329 ``duration @ N bpm`` + 32 ``duration @ N RPE`` (RPE steps
  carry NO zone label). Durations are ``N min``, ``N sec`` or ``M:SS``
  minutes:seconds.
- 329 zone lines, exactly one immediately after each bpm step, from a
  7-label vocabulary stored VERBATIM (``Zone 5B: Aerobic Capacity`` is the
  full label — a truncated ``grep -o`` extraction once corrupted this exact
  field in an earlier project; that is why zones are parsed from whole
  lines and the full-name test exists).
- 49 cadence lines: ``N-N rpm`` (41) and ``Nrpm`` (8), attached to the step
  they follow.
- 80 ``Repetir N veces`` markers (N in 1..6). Their repeat semantics are
  UNRESOLVED, so they are captured structurally as :class:`Repetition`
  items IN SEQUENCE and are NEVER expanded.
- 319 role lines (``Warm up``/``Active``/``Recovery``/``Cool down``) open
  blocks; 361 steps live in those 319 blocks, i.e. 42 steps inherit the
  role of the preceding step (run-length encoding in the source).
- 94 intent notes in 6 verbatim forms (``APRIETA``, ``A TOPE``, ``a tope``,
  ``aceleración``, ``NO TIENES QUE LLEGAR A ESTE PULSO``,
  ``NO PASA NADA SI NO LLEGAS AL PULSO MARCADO``), matched
  case-insensitively (accents preserved in the stored text). Notes are
  captured as POSITIONAL items exactly where they appear: the source does
  not say whether a note describes the preceding or the following step
  (e.g. ``A TOPE`` after a ``Repetir`` marker belongs to the upcoming
  step), so association is left to consumers instead of guessed here.

**Explicit surfacing.** Anything unparseable or ambiguous is recorded in
:data:`CyclingCorpus.issues` (source line + verbatim text + reason) —
never silently dropped. A line the grammar does not know inside a block is
BOTH captured verbatim as a non-intent :class:`Note` AND surfaced as an
issue, so no content can vanish. The full corpus parses with zero issues;
a non-zero issue list on new input is a signal to extend the grammar, not
to ignore the line.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final

__all__ = [
    "ZONE_VOCABULARY",
    "Block",
    "BlockItem",
    "BlockRole",
    "BpmTarget",
    "Cadence",
    "CyclingCorpus",
    "Duration",
    "Note",
    "ParseIssue",
    "Repetition",
    "RpeTarget",
    "Session",
    "Step",
    "Target",
    "parse_cycling_corpus",
]

# --- Source vocabulary (verbatim, as the file writes it) ---------------------

ZONE_VOCABULARY: Final[frozenset[str]] = frozenset(
    {
        "Zone 1: Recovery",
        "Zone 2: Aerobic",
        "Zone 3: Tempo",
        "Zone 4: SubThreshold",
        "Zone 5A: SuperThreshold",
        "Zone 5B: Aerobic Capacity",
        "Zone 5C: Anaerobic Capacity",
    }
)

_INTENT_NOTES: Final[frozenset[str]] = frozenset(
    {
        "aprieta",
        "a tope",
        "aceleracion",
        "no tienes que llegar a este pulso",
        "no pasa nada si no llegas al pulso marcado",
    }
)

_STEP_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<duration>\d+:\d{2}|\d+ (?:min|sec)) @ (?P<value>\d+) (?P<kind>bpm|RPE)$"
)
_ZONE_RE: Final[re.Pattern[str]] = re.compile(r"^Zone \d[A-C]?: .+$")
_CADENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<low>\d+)(?:-(?P<high>\d+))? ?rpm$"
)
_REPETITION_RE: Final[re.Pattern[str]] = re.compile(r"^Repetir (?P<times>\d+) veces$")
_LABELLED_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^E(?P<n>\d+):\s*(?P<rest>.*)$")


def _fold(text: str) -> str:
    """Case-fold and strip accents, for case-insensitive note matching only."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


# --- Data model (frozen; provenance via 1-based source line numbers) ---------


class BlockRole(Enum):
    """The four role lines that open a block, with the source's exact text."""

    WARM_UP = "Warm up"
    ACTIVE = "Active"
    RECOVERY = "Recovery"
    COOL_DOWN = "Cool down"


_ROLE_TEXTS: Final[dict[str, BlockRole]] = {role.value: role for role in BlockRole}


@dataclass(frozen=True, slots=True)
class Duration:
    """A step duration; ``seconds`` is normalized, ``raw`` is verbatim."""

    seconds: int
    raw: str


@dataclass(frozen=True, slots=True)
class BpmTarget:
    bpm: int


@dataclass(frozen=True, slots=True)
class RpeTarget:
    rpe: int


Target = BpmTarget | RpeTarget


@dataclass(frozen=True, slots=True)
class Cadence:
    """A cadence line; ``raw`` is verbatim (``70-70 rpm`` or ``90rpm``)."""

    rpm_low: int
    rpm_high: int
    raw: str


@dataclass(frozen=True, slots=True)
class Step:
    """One ``duration @ target`` line with its attached zone and cadence."""

    duration: Duration
    target: Target
    zone: str | None  # verbatim label, e.g. "Zone 5B: Aerobic Capacity"
    cadence: Cadence | None
    line: int  # 1-based source line of the step line itself


@dataclass(frozen=True, slots=True)
class Note:
    """A free-text line, kept verbatim and positional (association unguessed)."""

    text: str
    is_intent: bool  # True when it matches the known intent vocabulary
    line: int


@dataclass(frozen=True, slots=True)
class Repetition:
    """A ``Repetir N veces`` marker, captured structurally — never expanded."""

    times: int
    raw: str  # verbatim line, e.g. "Repetir 3 veces"
    line: int


BlockItem = Step | Note | Repetition


@dataclass(frozen=True, slots=True)
class Block:
    role: BlockRole
    items: tuple[BlockItem, ...]
    line: int  # 1-based source line of the role line


@dataclass(frozen=True, slots=True)
class Session:
    """One workout: labelled (E1..E5) or unlabelled; may be pure free text."""

    label: str | None  # "E1".."E5" or None
    description: str | None  # free-text sessions (E2/E4 style)
    blocks: tuple[Block, ...]
    line: int  # 1-based source line of the header (or first block)


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """A line that could not be parsed unambiguously — surfaced, never dropped."""

    line: int
    text: str  # verbatim source line
    reason: str


@dataclass(frozen=True, slots=True)
class CyclingCorpus:
    title: str | None
    sessions: tuple[Session, ...]
    issues: tuple[ParseIssue, ...]


# --- Parsing ------------------------------------------------------------------


def _parse_duration(raw: str) -> Duration:
    if ":" in raw:
        minutes_s, seconds_s = raw.split(":", maxsplit=1)
        return Duration(seconds=int(minutes_s) * 60 + int(seconds_s), raw=raw)
    value_s, unit = raw.split(" ", maxsplit=1)
    seconds = int(value_s) * (60 if unit == "min" else 1)
    return Duration(seconds=seconds, raw=raw)


def _split_regions(
    lines: list[str], first_lineno: int
) -> list[list[tuple[int, str]]]:
    """Split into ``--``-delimited regions, keeping absolute 1-based lines."""
    regions: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=first_lineno):
        stripped = line.strip()
        if stripped == "--":
            regions.append(current)
            current = []
        elif stripped:
            current.append((lineno, stripped))
    regions.append(current)  # trailing delimiter yields a trailing empty region
    return [region for region in regions if region]


def _step_from_match(match: re.Match[str], line: int) -> Step:
    kind = match.group("kind")
    value = int(match.group("value"))
    target: Target = BpmTarget(bpm=value) if kind == "bpm" else RpeTarget(rpe=value)
    return Step(
        duration=_parse_duration(match.group("duration")),
        target=target,
        zone=None,
        cadence=None,
        line=line,
    )


class _SessionBuilder:
    """Mutable accumulator for one session; freezes into the dataclasses."""

    def __init__(self, label: str | None, line: int, issues: list[ParseIssue]) -> None:
        self.label = label
        self.description: str | None = None
        self.line = line
        self.issues = issues  # shared collector for the whole corpus
        self._blocks: list[Block] = []
        self._role: BlockRole | None = None
        self._block_line = 0
        self._items: list[BlockItem] = []
        self._last_step_index: int | None = None  # index into _items, or None

    def _flush_block(self) -> None:
        if self._role is not None:
            self._blocks.append(
                Block(role=self._role, items=tuple(self._items), line=self._block_line)
            )
        self._role = None
        self._block_line = 0
        self._items = []
        self._last_step_index = None

    def _issue(self, line: int, text: str, reason: str) -> None:
        self.issues.append(ParseIssue(line=line, text=text, reason=reason))

    def add_line(self, line: int, text: str) -> None:
        role = _ROLE_TEXTS.get(text)
        if role is not None:
            self._flush_block()
            self._role = role
            self._block_line = line
            return

        step_match = _STEP_RE.match(text)
        if step_match is not None:
            if self._role is None:
                self._issue(line, text, "step before any block; not attached")
                return
            self._items.append(_step_from_match(step_match, line))
            self._last_step_index = len(self._items) - 1
            return

        if _ZONE_RE.match(text) is not None:
            if self._last_step_index is None:
                self._issue(line, text, "zone line with no preceding step in block")
                return
            last = self._items[self._last_step_index]
            assert isinstance(last, Step)
            if last.zone is not None:
                self._issue(line, text, "second zone line for the same step; first kept")
                return
            self._items[self._last_step_index] = replace(last, zone=text)
            if text not in ZONE_VOCABULARY:
                self._issue(line, text, "unknown zone label; kept verbatim")
            return

        cadence_match = _CADENCE_RE.match(text)
        if cadence_match is not None:
            if self._last_step_index is None:
                self._issue(line, text, "cadence line with no preceding step in block")
                return
            last = self._items[self._last_step_index]
            assert isinstance(last, Step)
            if last.cadence is not None:
                self._issue(
                    line, text, "second cadence line for the same step; first kept"
                )
                return
            low = int(cadence_match.group("low"))
            high_s = cadence_match.group("high")
            cadence = Cadence(
                rpm_low=low,
                rpm_high=int(high_s) if high_s is not None else low,
                raw=text,
            )
            self._items[self._last_step_index] = replace(last, cadence=cadence)
            return

        repetition_match = _REPETITION_RE.match(text)
        if repetition_match is not None:
            if self._role is None:
                self._issue(line, text, "repetition marker before any block")
                return
            self._items.append(
                Repetition(
                    times=int(repetition_match.group("times")), raw=text, line=line
                )
            )
            return

        # Anything else is free text: captured verbatim, positional. A line the
        # grammar does not recognize is ALSO surfaced as an issue so content
        # cannot vanish silently.
        if self._role is None:
            self._issue(line, text, "free-text line before any block; captured as issue")
            return
        is_intent = _fold(text) in _INTENT_NOTES
        self._items.append(Note(text=text, is_intent=is_intent, line=line))
        if not is_intent:
            self._issue(
                line, text, "unrecognized line; captured verbatim as a free-text note"
            )

    def finish(self) -> Session:
        self._flush_block()
        return Session(
            label=self.label,
            description=self.description,
            blocks=tuple(self._blocks),
            line=self.line,
        )


def _parse_labelled_session(
    region: list[tuple[int, str]], issues: list[ParseIssue]
) -> Session:
    first_line, first_text = region[0]
    header = _LABELLED_HEADER_RE.match(first_text)
    assert header is not None  # caller guarantees the shape
    builder = _SessionBuilder(label=f"E{header.group('n')}", line=first_line, issues=issues)
    rest = header.group("rest").strip()
    body = region[1:]
    if rest:
        builder.description = rest
    elif body and not any(
        _ROLE_TEXTS.get(text) is not None or _STEP_RE.match(text) for _, text in body
    ):
        # E2 style: the free-text description is the only content line.
        builder.description = body[0][1]
        for extra_line, extra_text in body[1:]:
            issues.append(
                ParseIssue(
                    line=extra_line,
                    text=extra_text,
                    reason="extra line in a free-text session; captured as issue",
                )
            )
        return builder.finish()
    for line, text in body:
        builder.add_line(line, text)
    return builder.finish()


def _parse_unlabelled_session(
    region: list[tuple[int, str]], issues: list[ParseIssue]
) -> Session:
    builder = _SessionBuilder(label=None, line=region[0][0], issues=issues)
    for line, text in region:
        builder.add_line(line, text)
    return builder.finish()


def _split_region_into_sessions(
    region: list[tuple[int, str]], issues: list[ParseIssue]
) -> list[Session]:
    """One region usually holds one session, BUT the labelled region of the
    real corpus packs E1..E5 into the first ``--``-delimited region: labelled
    sessions are delimited by their ``E<n>:`` headers inside it."""
    sessions: list[Session] = []
    chunk: list[tuple[int, str]] = []
    for entry in region:
        if _LABELLED_HEADER_RE.match(entry[1]) is not None:
            if chunk:
                sessions.append(_flush_chunk(chunk, issues))
            chunk = [entry]
        else:
            chunk.append(entry)
    if chunk:
        sessions.append(_flush_chunk(chunk, issues))
    return sessions


def _flush_chunk(chunk: list[tuple[int, str]], issues: list[ParseIssue]) -> Session:
    if _LABELLED_HEADER_RE.match(chunk[0][1]) is not None:
        return _parse_labelled_session(chunk, issues)
    return _parse_unlabelled_session(chunk, issues)


def parse_cycling_corpus(text: str) -> CyclingCorpus:
    """Parse the cycling corpus text into typed, frozen data.

    Pure function: no I/O. Line numbers in the result refer to ``text`` as
    given (1-based, after CRLF normalization). Anything unparseable or
    ambiguous is recorded in :attr:`CyclingCorpus.issues`.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    title: str | None = None
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            if stripped.endswith(":") and _LABELLED_HEADER_RE.match(stripped) is None:
                title = stripped
                start = index + 1
            else:
                start = index
            break

    issues: list[ParseIssue] = []
    sessions: list[Session] = []
    for region in _split_regions(lines[start:], first_lineno=start + 1):
        sessions.extend(_split_region_into_sessions(region, issues))

    return CyclingCorpus(title=title, sessions=tuple(sessions), issues=tuple(issues))
