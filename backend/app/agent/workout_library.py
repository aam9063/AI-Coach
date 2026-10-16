# This module stores Spanish workout notation VERBATIM (the prime character
# U+00B4 in rest/hold lines, curly U+201D quotes in CORE holds) in regexes,
# docstrings, and strings — exactly as the source files write them. Ruff's
# ambiguous-character rules (RUF001/2/3) false-positive on those on purpose;
# swapping them for look-alike ASCII would misrepresent the corpus.
# Suppressed at file level, nothing else.
# ruff: noqa: RUF001, RUF002, RUF003

"""Pure parsers for the cycling and gym workout corpora (WLK-2/WLK-3) plus the
canonical JSON seam (WLK-4) and the typed loader (WLK-5); ODD feature
workout-library.

**Provenance.** Parses the owner's reference files
``docs/exercises/EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt`` (1,727 lines,
CRLF endings) and ``docs/exercises/gym.txt`` (61 lines, CRLF endings, no
trailing newline) into typed, frozen data. Both parsers are PURE text ->
data: no file I/O, no clock, no network. I/O belongs to the loader and the
fixture generation script (WLK-4/WLK-5), mirroring the engine purity
conventions.

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

**Measured grammar of gym.txt** (re-measured by hand; see
``tests/agent/test_workout_library_gym.py`` for the accounting regression
that enforces every number — it deliberately DIFFERS from an earlier
project's hypothesis about a file with this name):

- 61 lines, CRLF, no trailing newline, and NO trailing ``--`` delimiter
  (unlike the cycling corpus). Line 1 is a title; two ``--`` lines split
  the rest into exactly 3 focus sections: TREN INFERIOR, TREN SUPERIOR,
  CORE (headers may carry a ``- `` prefix; the name is stored without it).
- A 4th header, ``ACTIVACIÓN:``, opens a sub-block INSIDE TREN INFERIOR.
- 18 sets-x-reps exercises: ``name: NxM`` with per-set or range tokens
  separated by ``-`` (``5x20-15-15-10-10``) or ``/`` (``3x8/10``); the
  token list is stored VERBATIM and never interpreted as per-set vs range
  (the source is ambiguous: ``4x25-30`` is a range, ``5x20-15-15-10-10``
  is per-set). Name quirks preserved: ``Crunch de abdomen:4x25-30.`` (no
  space after the colon), ``-Press banca--> 4x13,`` (decoration stripped,
  trailing comma dropped), the ``Pres banca inclinado`` typo, uppercase
  ``3X12``, and the scheme-first form ``3x25 crunch abdomen``.
- RIR is OPTIONAL: ``RIR N`` trailing an exercise is extracted (3
  exercises state it); everything else in the trailing text becomes the
  exercise note (tempo notes like the 4-second-eccentric text kept
  verbatim).
- 3 Spanish rest lines: ``N´ [N´´] [DE] REC[.]`` — ``1´ 30´´ REC`` is 90
  seconds; the trailing-period variant ``2´ DE REC.`` is tolerated.
- 7 holds: ``3 planchas de {N minuto|N´´}`` (rounds form), ``NAME N”
  [cada lado]`` and ``NAME N”/lado`` (curly U+201D seconds), and ``NAME
  N reps/pierna`` (also compact ``10reps/pierna``).
- 4 prose lines. Prose is a RECOGNIZED category (captured verbatim, no
  issue) — but a line with digits that matches no structured grammar is
  ALSO surfaced as an issue, because it almost certainly carries training
  data the grammar missed (the corpus has exactly two: the warm-up line
  ``10´ de calentamiento ...`` on line 4 and the ``10 PASOS`` activation
  prescription on line 8).

**Explicit surfacing.** Anything unparseable or ambiguous is recorded in
:data:`CyclingCorpus.issues` / :data:`GymCorpus.issues` (source line +
verbatim text + reason) — never silently dropped. In the cycling corpus a
line the grammar does not know inside a block is BOTH captured verbatim as
a non-intent :class:`Note` AND surfaced as an issue, so no content can
vanish. Both full corpora parse with zero cycling issues and exactly two
known gym issues; a non-zero issue list on new input is a signal to extend
the grammar, not to ignore the line.

**Canonical JSON (WLK-4).** :func:`cycling_corpus_to_dict` and
:func:`gym_corpus_to_dict` project the dataclasses onto JSON-safe dicts
(discriminated by ``kind``), :func:`to_canonical_json` renders them with
sorted keys, 2-space indent, ``ensure_ascii=False`` (accents stay
readable) and a trailing newline. The committed fixtures in
``backend/data/exercises/`` are GENERATED artifacts (by
``backend/scripts/generate_workout_fixtures.py``, which owns the only I/O
besides the loader) and the round-trip regression test proves committed
JSON == a fresh parse.

**Loader seam (WLK-5).** :func:`load_workout_library` reads the canonical
JSON fixtures and returns a typed :class:`WorkoutLibrary` with lookup by
id and by name. IDs are deterministic (parse order): ``cycling-E1`` for
labelled sessions, ``cycling-session-NN`` for unlabelled ones,
``gym-section-NN`` for gym sections. This is the seam a later session-
generation feature will consume: iterate :attr:`WorkoutLibrary.entries`
(or look up a stable id), read the typed :class:`Session` /
:class:`GymSection` content, and expand it into engine sessions — the
library never generates sessions itself.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final

__all__ = [
    "CYCLING_FIXTURE_PATH",
    "GYM_FIXTURE_PATH",
    "ZONE_VOCABULARY",
    "Block",
    "BlockItem",
    "BlockRole",
    "BpmTarget",
    "Cadence",
    "CyclingCorpus",
    "Duration",
    "GymActivationBlock",
    "GymCorpus",
    "GymExercise",
    "GymHold",
    "GymItem",
    "GymProse",
    "GymRest",
    "GymSection",
    "Note",
    "ParseIssue",
    "Repetition",
    "RpeTarget",
    "Session",
    "Step",
    "Target",
    "WorkoutEntry",
    "WorkoutLibrary",
    "build_workout_library",
    "cycling_corpus_from_dict",
    "cycling_corpus_to_dict",
    "gym_corpus_from_dict",
    "gym_corpus_to_dict",
    "load_workout_library",
    "parse_cycling_corpus",
    "parse_gym_corpus",
    "to_canonical_json",
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


def _leading_title(
    lines: list[str], *, is_header: Callable[[str], bool] | None = None
) -> tuple[str | None, int]:
    """The first non-blank line ending with ``:`` is a corpus title (unless
    ``is_header`` matches it); returns ``(title, index of first body line)``."""
    title: str | None = None
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            if stripped.endswith(":") and (is_header is None or not is_header(stripped)):
                title = stripped
                start = index + 1
            else:
                start = index
            break
    return title, start


def parse_cycling_corpus(text: str) -> CyclingCorpus:
    """Parse the cycling corpus text into typed, frozen data.

    Pure function: no I/O. Line numbers in the result refer to ``text`` as
    given (1-based, after CRLF normalization). Anything unparseable or
    ambiguous is recorded in :attr:`CyclingCorpus.issues`.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    title, start = _leading_title(
        lines, is_header=lambda text: _LABELLED_HEADER_RE.match(text) is not None
    )

    issues: list[ParseIssue] = []
    sessions: list[Session] = []
    for region in _split_regions(lines[start:], first_lineno=start + 1):
        sessions.extend(_split_region_into_sessions(region, issues))

    return CyclingCorpus(title=title, sessions=tuple(sessions), issues=tuple(issues))


# --- Gym corpus (WLK-3) -------------------------------------------------------

# Spanish rest notation: `1´ 30´´ REC`, `2´ DE REC`, `2´ DE REC.`
_GYM_REST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<minutes>\d+)´(?:\s*(?P<seconds>\d+)´´)?\s+(?:DE\s+)?REC\.?$"
)
# Sets x reps scheme: `4x13`, `5x20-15-15-10-10`, `3X12`, `3x8/10`.
_GYM_SCHEME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<sets>\d+)[xX](?P<reps>\d+(?:[-/]\d+)*)"
)
_GYM_RIR_RE: Final[re.Pattern[str]] = re.compile(r"\bRIR\s+(?P<rir>\d+)")
# Core holds: `NAME 15” cada lado` / `NAME 15”/lado` (U+201D seconds).
_GYM_HOLD_QUOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>.+?)\s+(?P<seconds>\d+)”"
    r"(?:/(?P<per_slash>\w+))?(?:\s+cada\s+(?P<per_cada>\w+))?[.:]?$"
)
# `NAME 10 reps/pierna` and the compact `NAME 10reps/pierna`.
_GYM_HOLD_REPS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>.+?)\s+(?P<reps>\d+)\s*reps?/(?P<per>\w+)[.:]?$"
)
# Rounds form: `3 planchas de 1 minuto.` / `3 planchas de 30´´`.
_GYM_HOLD_ROUNDS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<rounds>\d+)\s+(?P<name>.+?)\s+de\s+"
    r"(?:(?P<minutes>\d+)\s+minuto(?:s)?|(?P<seconds>\d+)´´)[.:]?$"
)


@dataclass(frozen=True, slots=True)
class GymExercise:
    """One sets-x-reps entry. ``reps_raw``/``reps`` are the VERBATIM tokens
    after ``x`` — the source is ambiguous between per-set lists
    (``5x20-15-15-10-10``) and ranges (``4x25-30``), so interpretation is
    left to consumers. RIR is OPTIONAL (``None`` when the entry states none)."""

    name: str
    sets: int
    reps_raw: str
    reps: tuple[int, ...]
    rir: int | None
    note: str | None  # trailing free text (tempo notes, cues), trimmed
    line: int  # 1-based source line


@dataclass(frozen=True, slots=True)
class GymHold:
    """A hold or per-side prescription (CORE blocks, plancha rounds).

    Exactly one of ``seconds``/``reps`` is set; ``per`` is the verbatim
    per-side qualifier (``cada lado``, ``lado``, ``pierna``); ``rounds`` is
    set only by the ``3 planchas de ...`` form."""

    name: str
    rounds: int | None
    seconds: int | None
    reps: int | None
    per: str | None
    raw: str  # verbatim line
    line: int


@dataclass(frozen=True, slots=True)
class GymProse:
    """A free-text line, kept verbatim. Prose is a recognized category, so
    it is NOT an issue by itself — but a prose line containing digits is
    also surfaced as a :class:`ParseIssue` (it almost certainly carries
    training data the grammar missed)."""

    text: str
    line: int


@dataclass(frozen=True, slots=True)
class GymRest:
    """A Spanish-notation rest line; reuses :class:`Duration` (seconds
    normalized, ``raw`` is the full verbatim rest line, e.g. ``1´ 30´´ REC``)."""

    duration: Duration
    line: int


@dataclass(frozen=True, slots=True)
class GymActivationBlock:
    """A sub-block header inside a section (``ACTIVACIÓN:`` in the real
    corpus). Items attach to it until the section ends."""

    name: str  # verbatim header name
    items: tuple[GymItem, ...]
    line: int


GymItem = GymExercise | GymHold | GymProse | GymRest | GymActivationBlock


@dataclass(frozen=True, slots=True)
class GymSection:
    """One ``--``-delimited focus section; ``name`` is the verbatim header
    (``None`` for a region with no header line)."""

    name: str | None
    items: tuple[GymItem, ...]
    line: int  # 1-based source line of the header (or first item)


@dataclass(frozen=True, slots=True)
class GymCorpus:
    title: str | None
    sections: tuple[GymSection, ...]
    issues: tuple[ParseIssue, ...]


def _gym_header_name(text: str) -> str | None:
    """A section header ends with ``:`` and carries no digits (``- TREN
    INFERIOR:``, ``ACTIVACIÓN:``); returns the name without decoration."""
    if not text.endswith(":") or any(ch.isdigit() for ch in text):
        return None
    return text.lstrip("- ").rstrip(":").strip() or None


def _gym_clean_name(raw: str) -> str:
    """Strip the source's name decorations (``-Press banca-->`` and the
    ``name:`` colon) while keeping the text itself verbatim."""
    name = raw.strip().lstrip("-").strip()
    if name.endswith("-->"):
        name = name[:-3].strip()
    return name.rstrip(":").strip()


def _gym_exercise_from_line(line_no: int, text: str) -> GymExercise:
    match = _GYM_SCHEME_RE.search(text)
    assert match is not None  # caller guarantees the shape
    sets = int(match.group("sets"))
    reps_raw = match.group("reps")
    reps = tuple(int(token) for token in re.split(r"[-/]", reps_raw))
    prefix = text[: match.start()].strip()
    suffix = text[match.end() :].strip()
    if not prefix:  # scheme-first form: `3x25 crunch abdomen`
        return GymExercise(
            name=suffix.strip(" \t.,;"),
            sets=sets,
            reps_raw=reps_raw,
            reps=reps,
            rir=None,
            note=None,
            line=line_no,
        )
    rir_match = _GYM_RIR_RE.search(suffix)
    note_material = suffix
    if rir_match is not None:
        note_material = suffix[: rir_match.start()] + suffix[rir_match.end() :]
    return GymExercise(
        name=_gym_clean_name(prefix),
        sets=sets,
        reps_raw=reps_raw,
        reps=reps,
        rir=int(rir_match.group("rir")) if rir_match is not None else None,
        note=note_material.strip(" \t.,;") or None,
        line=line_no,
    )


def _gym_hold_from_line(line_no: int, text: str) -> GymHold | None:
    quote = _GYM_HOLD_QUOTE_RE.match(text)
    if quote is not None:
        per_cada = quote.group("per_cada")
        return GymHold(
            name=quote.group("name"),
            rounds=None,
            seconds=int(quote.group("seconds")),
            reps=None,
            per=quote.group("per_slash") or (f"cada {per_cada}" if per_cada else None),
            raw=text,
            line=line_no,
        )
    reps = _GYM_HOLD_REPS_RE.match(text)
    if reps is not None:
        return GymHold(
            name=reps.group("name"),
            rounds=None,
            seconds=None,
            reps=int(reps.group("reps")),
            per=reps.group("per"),
            raw=text,
            line=line_no,
        )
    rounds = _GYM_HOLD_ROUNDS_RE.match(text)
    if rounds is not None:
        minutes = rounds.group("minutes")
        seconds_s = rounds.group("seconds")
        return GymHold(
            name=rounds.group("name"),
            rounds=int(rounds.group("rounds")),
            seconds=int(minutes) * 60 if minutes is not None else int(seconds_s or 0),
            reps=None,
            per=None,
            raw=text,
            line=line_no,
        )
    return None


def _parse_gym_line(line_no: int, text: str, issues: list[ParseIssue]) -> GymItem:
    rest = _GYM_REST_RE.match(text)
    if rest is not None:
        seconds = int(rest.group("seconds") or 0)
        return GymRest(
            duration=Duration(
                seconds=int(rest.group("minutes")) * 60 + seconds, raw=text
            ),
            line=line_no,
        )
    if _GYM_SCHEME_RE.search(text) is not None:
        return _gym_exercise_from_line(line_no, text)
    hold = _gym_hold_from_line(line_no, text)
    if hold is not None:
        return hold
    # Prose is a recognized category — but digits mean the grammar likely
    # missed training data, so surface it explicitly too.
    if any(ch.isdigit() for ch in text):
        issues.append(
            ParseIssue(
                line=line_no,
                text=text,
                reason="unmatched line containing digits; captured verbatim as prose",
            )
        )
    return GymProse(text=text, line=line_no)


def _split_gym_regions(
    lines: list[str], first_lineno: int
) -> list[list[tuple[int, str]]]:
    """Like :func:`_split_regions`, but KEEPS blank lines (as ``""``): the
    gym grammar uses a blank line to close an ACTIVACIÓN sub-block, since
    the source delimits it by whitespace, not by any marker."""
    regions: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=first_lineno):
        stripped = line.strip()
        if stripped == "--":
            regions.append(current)
            current = []
        else:
            current.append((lineno, stripped))  # blanks kept as ""
    regions.append(current)
    return [region for region in regions if any(text for _, text in region)]


def _parse_gym_region(
    region: list[tuple[int, str]], issues: list[ParseIssue]
) -> GymSection:
    content = [(line_no, text) for line_no, text in region if text]
    if not content:
        return GymSection(name=None, items=(), line=region[0][0])
    first_line, first_text = content[0]
    header = _gym_header_name(first_text)
    if header is not None:
        # Keep blanks in the body: a blank line closes an open activation
        # sub-block (the source delimits it by whitespace, not by a marker).
        body = [(line_no, text) for line_no, text in region if line_no > first_line]
    else:
        body = region
    items: list[GymItem] = []
    activation_name: str | None = None
    activation_line = 0
    activation_items: list[GymItem] = []

    def flush_activation() -> None:
        nonlocal activation_name, activation_items
        if activation_name is not None:
            items.append(
                GymActivationBlock(
                    name=activation_name,
                    items=tuple(activation_items),
                    line=activation_line,
                )
            )
            activation_name = None
            activation_items = []

    for line_no, text in body:
        if not text:  # blank line: closes an open activation sub-block
            flush_activation()
            continue
        header_name = _gym_header_name(text)
        if header_name is not None:
            if activation_name is not None:
                issues.append(
                    ParseIssue(
                        line=line_no,
                        text=text,
                        reason="second sub-block header inside one section; captured as issue",
                    )
                )
            else:
                activation_name = header_name
                activation_line = line_no
            continue
        if activation_name is not None:
            activation_items.append(_parse_gym_line(line_no, text, issues))
        else:
            items.append(_parse_gym_line(line_no, text, issues))
    flush_activation()
    return GymSection(name=header, items=tuple(items), line=first_line)


def parse_gym_corpus(text: str) -> GymCorpus:
    """Parse the gym corpus text into typed, frozen data.

    Pure function: no I/O. Line numbers in the result refer to ``text`` as
    given (1-based, after CRLF normalization). Anything unparseable or
    ambiguous is recorded in :attr:`GymCorpus.issues`.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title, start = _leading_title(lines)

    issues: list[ParseIssue] = []
    sections = [
        _parse_gym_region(region, issues)
        for region in _split_gym_regions(lines[start:], first_lineno=start + 1)
    ]
    return GymCorpus(title=title, sections=tuple(sections), issues=tuple(issues))


# --- Canonical JSON (WLK-4) ---------------------------------------------------
# to_dict/from_dict are PURE projections between the dataclasses and JSON-safe
# dicts (items discriminated by "kind"); the generation script and the loader
# own the only I/O. from_dict validates shapes and raises ValueError on
# anything the to_dict projection cannot produce.


def to_canonical_json(value: dict[str, object]) -> str:
    """Canonical rendering: sorted keys, 2-space indent, accents readable,
    trailing newline. Both fixtures and the round-trip test use exactly this."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _json_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{context}: expected a string, got {type(value).__name__}")
    return value


def _json_opt_str(value: object, context: str) -> str | None:
    return None if value is None else _json_str(value, context)


def _json_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context}: expected an int, got {type(value).__name__}")
    return value


def _json_opt_int(value: object, context: str) -> int | None:
    return None if value is None else _json_int(value, context)


def _json_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context}: expected a bool, got {type(value).__name__}")
    return value


def _json_dict(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected an object, got {type(value).__name__}")
    return value


def _json_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context}: expected an array, got {type(value).__name__}")
    return value


# --- Cycling corpus <-> dict --------------------------------------------------


def _cycling_target_to_dict(target: Target) -> dict[str, object]:
    if isinstance(target, BpmTarget):
        return {"kind": "bpm", "bpm": target.bpm}
    return {"kind": "rpe", "rpe": target.rpe}


def _cycling_target_from_dict(value: object, context: str) -> Target:
    target = _json_dict(value, context)
    kind = _json_str(target.get("kind"), f"{context}.kind")
    if kind == "bpm":
        return BpmTarget(bpm=_json_int(target.get("bpm"), f"{context}.bpm"))
    if kind == "rpe":
        return RpeTarget(rpe=_json_int(target.get("rpe"), f"{context}.rpe"))
    raise ValueError(f"{context}.kind: unknown target kind {kind!r}")


def _cycling_item_to_dict(item: BlockItem) -> dict[str, object]:
    if isinstance(item, Step):
        cadence: dict[str, object] | None = None
        if item.cadence is not None:
            cadence = {
                "rpm_low": item.cadence.rpm_low,
                "rpm_high": item.cadence.rpm_high,
                "raw": item.cadence.raw,
            }
        return {
            "kind": "step",
            "duration": {"seconds": item.duration.seconds, "raw": item.duration.raw},
            "target": _cycling_target_to_dict(item.target),
            "zone": item.zone,
            "cadence": cadence,
            "line": item.line,
        }
    if isinstance(item, Note):
        return {
            "kind": "note",
            "text": item.text,
            "is_intent": item.is_intent,
            "line": item.line,
        }
    return {
        "kind": "repetition",
        "times": item.times,
        "raw": item.raw,
        "line": item.line,
    }


def _cycling_item_from_dict(value: object, context: str) -> BlockItem:
    item = _json_dict(value, context)
    kind = _json_str(item.get("kind"), f"{context}.kind")
    if kind == "step":
        duration = _json_dict(item.get("duration"), f"{context}.duration")
        cadence_value = item.get("cadence")
        cadence: Cadence | None = None
        if cadence_value is not None:
            cadence_dict = _json_dict(cadence_value, f"{context}.cadence")
            cadence = Cadence(
                rpm_low=_json_int(
                    cadence_dict.get("rpm_low"), f"{context}.cadence.rpm_low"
                ),
                rpm_high=_json_int(
                    cadence_dict.get("rpm_high"), f"{context}.cadence.rpm_high"
                ),
                raw=_json_str(cadence_dict.get("raw"), f"{context}.cadence.raw"),
            )
        return Step(
            duration=Duration(
                seconds=_json_int(
                    duration.get("seconds"), f"{context}.duration.seconds"
                ),
                raw=_json_str(duration.get("raw"), f"{context}.duration.raw"),
            ),
            target=_cycling_target_from_dict(item.get("target"), f"{context}.target"),
            zone=_json_opt_str(item.get("zone"), f"{context}.zone"),
            cadence=cadence,
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    if kind == "note":
        return Note(
            text=_json_str(item.get("text"), f"{context}.text"),
            is_intent=_json_bool(item.get("is_intent"), f"{context}.is_intent"),
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    if kind == "repetition":
        return Repetition(
            times=_json_int(item.get("times"), f"{context}.times"),
            raw=_json_str(item.get("raw"), f"{context}.raw"),
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    raise ValueError(f"{context}.kind: unknown block item kind {kind!r}")


def cycling_corpus_to_dict(corpus: CyclingCorpus) -> dict[str, object]:
    """Project a :class:`CyclingCorpus` onto a JSON-safe dict (canonical)."""
    return {
        "schema_version": 1,
        "kind": "cycling-corpus",
        "title": corpus.title,
        "sessions": [
            {
                "label": session.label,
                "description": session.description,
                "line": session.line,
                "blocks": [
                    {
                        "role": block.role.value,
                        "line": block.line,
                        "items": [_cycling_item_to_dict(item) for item in block.items],
                    }
                    for block in session.blocks
                ],
            }
            for session in corpus.sessions
        ],
        "issues": [
            {"line": issue.line, "text": issue.text, "reason": issue.reason}
            for issue in corpus.issues
        ],
    }


def cycling_corpus_from_dict(value: object) -> CyclingCorpus:
    """Rebuild a :class:`CyclingCorpus` from its canonical dict projection."""
    corpus = _json_dict(value, "cycling corpus")
    version = _json_int(corpus.get("schema_version"), "cycling schema_version")
    if version != 1:
        raise ValueError(f"cycling schema_version: expected 1, got {version}")
    sessions: list[Session] = []
    for i, session_value in enumerate(
        _json_list(corpus.get("sessions"), "cycling sessions")
    ):
        session_dict = _json_dict(session_value, f"sessions[{i}]")
        blocks: list[Block] = []
        for j, block_value in enumerate(
            _json_list(session_dict.get("blocks"), f"sessions[{i}].blocks")
        ):
            block_dict = _json_dict(block_value, f"sessions[{i}].blocks[{j}]")
            role_text = _json_str(
                block_dict.get("role"), f"sessions[{i}].blocks[{j}].role"
            )
            role = _ROLE_TEXTS.get(role_text)
            if role is None:
                raise ValueError(f"sessions[{i}].blocks[{j}].role: unknown role")
            items: list[BlockItem] = []
            for k, item_value in enumerate(
                _json_list(block_dict.get("items"), f"sessions[{i}].blocks[{j}].items")
            ):
                items.append(
                    _cycling_item_from_dict(
                        item_value, f"sessions[{i}].blocks[{j}].items[{k}]"
                    )
                )
            blocks.append(
                Block(
                    role=role,
                    line=_json_int(
                        block_dict.get("line"), f"sessions[{i}].blocks[{j}].line"
                    ),
                    items=tuple(items),
                )
            )
        sessions.append(
            Session(
                label=_json_opt_str(session_dict.get("label"), f"sessions[{i}].label"),
                description=_json_opt_str(
                    session_dict.get("description"), f"sessions[{i}].description"
                ),
                line=_json_int(session_dict.get("line"), f"sessions[{i}].line"),
                blocks=tuple(blocks),
            )
        )
    issues: list[ParseIssue] = []
    for i, issue_value in enumerate(_json_list(corpus.get("issues"), "cycling issues")):
        issue_dict = _json_dict(issue_value, f"issues[{i}]")
        issues.append(
            ParseIssue(
                line=_json_int(issue_dict.get("line"), f"issues[{i}].line"),
                text=_json_str(issue_dict.get("text"), f"issues[{i}].text"),
                reason=_json_str(issue_dict.get("reason"), f"issues[{i}].reason"),
            )
        )
    return CyclingCorpus(
        title=_json_opt_str(corpus.get("title"), "cycling title"),
        sessions=tuple(sessions),
        issues=tuple(issues),
    )


# --- Gym corpus <-> dict ------------------------------------------------------


def _gym_item_to_dict(item: GymItem) -> dict[str, object]:
    if isinstance(item, GymExercise):
        return {
            "kind": "exercise",
            "name": item.name,
            "sets": item.sets,
            "reps_raw": item.reps_raw,
            "reps": list(item.reps),
            "rir": item.rir,
            "note": item.note,
            "line": item.line,
        }
    if isinstance(item, GymHold):
        return {
            "kind": "hold",
            "name": item.name,
            "rounds": item.rounds,
            "seconds": item.seconds,
            "reps": item.reps,
            "per": item.per,
            "raw": item.raw,
            "line": item.line,
        }
    if isinstance(item, GymProse):
        return {"kind": "prose", "text": item.text, "line": item.line}
    if isinstance(item, GymRest):
        return {
            "kind": "rest",
            "seconds": item.duration.seconds,
            "raw": item.duration.raw,
            "line": item.line,
        }
    return {
        "kind": "activation",
        "name": item.name,
        "line": item.line,
        "items": [_gym_item_to_dict(nested) for nested in item.items],
    }


def _gym_item_from_dict(value: object, context: str) -> GymItem:
    item = _json_dict(value, context)
    kind = _json_str(item.get("kind"), f"{context}.kind")
    if kind == "exercise":
        return GymExercise(
            name=_json_str(item.get("name"), f"{context}.name"),
            sets=_json_int(item.get("sets"), f"{context}.sets"),
            reps_raw=_json_str(item.get("reps_raw"), f"{context}.reps_raw"),
            reps=tuple(
                _json_int(rep, f"{context}.reps[{i}]")
                for i, rep in enumerate(
                    _json_list(item.get("reps"), f"{context}.reps")
                )
            ),
            rir=_json_opt_int(item.get("rir"), f"{context}.rir"),
            note=_json_opt_str(item.get("note"), f"{context}.note"),
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    if kind == "hold":
        return GymHold(
            name=_json_str(item.get("name"), f"{context}.name"),
            rounds=_json_opt_int(item.get("rounds"), f"{context}.rounds"),
            seconds=_json_opt_int(item.get("seconds"), f"{context}.seconds"),
            reps=_json_opt_int(item.get("reps"), f"{context}.reps"),
            per=_json_opt_str(item.get("per"), f"{context}.per"),
            raw=_json_str(item.get("raw"), f"{context}.raw"),
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    if kind == "prose":
        return GymProse(
            text=_json_str(item.get("text"), f"{context}.text"),
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    if kind == "rest":
        return GymRest(
            duration=Duration(
                seconds=_json_int(item.get("seconds"), f"{context}.seconds"),
                raw=_json_str(item.get("raw"), f"{context}.raw"),
            ),
            line=_json_int(item.get("line"), f"{context}.line"),
        )
    if kind == "activation":
        return GymActivationBlock(
            name=_json_str(item.get("name"), f"{context}.name"),
            line=_json_int(item.get("line"), f"{context}.line"),
            items=tuple(
                _gym_item_from_dict(nested, f"{context}.items[{i}]")
                for i, nested in enumerate(
                    _json_list(item.get("items"), f"{context}.items")
                )
            ),
        )
    raise ValueError(f"{context}.kind: unknown gym item kind {kind!r}")


def gym_corpus_to_dict(corpus: GymCorpus) -> dict[str, object]:
    """Project a :class:`GymCorpus` onto a JSON-safe dict (canonical)."""
    return {
        "schema_version": 1,
        "kind": "gym-corpus",
        "title": corpus.title,
        "sections": [
            {
                "name": section.name,
                "line": section.line,
                "items": [_gym_item_to_dict(item) for item in section.items],
            }
            for section in corpus.sections
        ],
        "issues": [
            {"line": issue.line, "text": issue.text, "reason": issue.reason}
            for issue in corpus.issues
        ],
    }


def gym_corpus_from_dict(value: object) -> GymCorpus:
    """Rebuild a :class:`GymCorpus` from its canonical dict projection."""
    corpus = _json_dict(value, "gym corpus")
    version = _json_int(corpus.get("schema_version"), "gym schema_version")
    if version != 1:
        raise ValueError(f"gym schema_version: expected 1, got {version}")
    sections: list[GymSection] = []
    for i, section_value in enumerate(_json_list(corpus.get("sections"), "gym sections")):
        section_dict = _json_dict(section_value, f"sections[{i}]")
        items: list[GymItem] = []
        for j, item_value in enumerate(
            _json_list(section_dict.get("items"), f"sections[{i}].items")
        ):
            items.append(_gym_item_from_dict(item_value, f"sections[{i}].items[{j}]"))
        sections.append(
            GymSection(
                name=_json_opt_str(section_dict.get("name"), f"sections[{i}].name"),
                line=_json_int(section_dict.get("line"), f"sections[{i}].line"),
                items=tuple(items),
            )
        )
    issues: list[ParseIssue] = []
    for i, issue_value in enumerate(_json_list(corpus.get("issues"), "gym issues")):
        issue_dict = _json_dict(issue_value, f"issues[{i}]")
        issues.append(
            ParseIssue(
                line=_json_int(issue_dict.get("line"), f"issues[{i}].line"),
                text=_json_str(issue_dict.get("text"), f"issues[{i}].text"),
                reason=_json_str(issue_dict.get("reason"), f"issues[{i}].reason"),
            )
        )
    return GymCorpus(
        title=_json_opt_str(corpus.get("title"), "gym title"),
        sections=tuple(sections),
        issues=tuple(issues),
    )


# --- Typed loader (WLK-5) -----------------------------------------------------
# The only I/O in this module lives here. See the module docstring for the
# provenance of the fixtures and the session-generation seam.


_BACKEND_DIR: Final[Path] = Path(__file__).resolve().parents[2]
CYCLING_FIXTURE_PATH: Final[Path] = _BACKEND_DIR / "data" / "exercises" / "cycling.json"
GYM_FIXTURE_PATH: Final[Path] = _BACKEND_DIR / "data" / "exercises" / "gym.json"


@dataclass(frozen=True, slots=True)
class WorkoutEntry:
    """One reference workout. ``id`` is deterministic (parse order);
    ``name`` is the cycling session label (``E1``..``E5``) or the gym
    section name, and is ``None`` for unlabelled cycling sessions."""

    id: str
    source: str  # "cycling" | "gym"
    name: str | None
    content: Session | GymSection


@dataclass(frozen=True, slots=True)
class WorkoutLibrary:
    """The typed data bank behind the agent's reference material and the
    seam a later session-generation feature will consume."""

    entries: tuple[WorkoutEntry, ...]

    def by_id(self, workout_id: str) -> WorkoutEntry | None:
        for entry in self.entries:
            if entry.id == workout_id:
                return entry
        return None

    def by_name(self, name: str) -> tuple[WorkoutEntry, ...]:
        return tuple(entry for entry in self.entries if entry.name == name)


def build_workout_library(cycling: CyclingCorpus, gym: GymCorpus) -> WorkoutLibrary:
    """Assemble the library from parsed corpora (pure; ids are deterministic
    from parse order, so the committed fixtures give stable ids)."""
    entries: list[WorkoutEntry] = []
    unlabelled = 0
    for session in cycling.sessions:
        if session.label is None:
            unlabelled += 1
            entries.append(
                WorkoutEntry(
                    id=f"cycling-session-{unlabelled:02d}",
                    source="cycling",
                    name=None,
                    content=session,
                )
            )
        else:
            entries.append(
                WorkoutEntry(
                    id=f"cycling-{session.label}",
                    source="cycling",
                    name=session.label,
                    content=session,
                )
            )
    for ordinal, section in enumerate(gym.sections, start=1):
        entries.append(
            WorkoutEntry(
                id=f"gym-section-{ordinal:02d}",
                source="gym",
                name=section.name,
                content=section,
            )
        )
    return WorkoutLibrary(entries=tuple(entries))


def load_workout_library(
    cycling_path: Path = CYCLING_FIXTURE_PATH, gym_path: Path = GYM_FIXTURE_PATH
) -> WorkoutLibrary:
    """Read the canonical JSON fixtures and return the typed library.

    This is the ONLY function here that touches the filesystem: the parse
    functions above stay pure text -> data."""
    cycling = cycling_corpus_from_dict(
        json.loads(cycling_path.read_text(encoding="utf-8"))
    )
    gym = gym_corpus_from_dict(json.loads(gym_path.read_text(encoding="utf-8")))
    return build_workout_library(cycling, gym)
