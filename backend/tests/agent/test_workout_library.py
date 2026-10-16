"""Cycling corpus parser tests (WLK-1) — real excerpts, measured grammar.

Every excerpt below is copied VERBATIM from
``docs/exercises/EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt`` (LF-normalized
for readability; the real file has CRLF endings, which the full-corpus test
exercises). The grammar was re-measured against THIS file by hand (whole-line
matching only — the project's known `grep -o` truncation trap is why zone
labels are asserted as full strings):

- 1 title line, 5 labelled sessions (E1..E5; E2/E4 free text), 24 unlabelled
  sessions separated by lines that are exactly ``--`` (a trailing delimiter
  ends the file; it must not produce a phantom session).
- 361 steps = 329 ``duration @ N bpm`` + 32 ``duration @ N RPE``; durations
  are ``N min``, ``N sec`` (64) or ``M:SS`` mm:ss (33).
- 329 zone lines, one per bpm step, all from the 7-label verbatim vocabulary.
- 49 cadence lines: ``N-N rpm`` (41) and ``Nrpm`` (8).
- 80 ``Repetir N veces`` markers, N in 1..6 — captured structurally, NEVER
  expanded.
- 319 role lines (Warm up 27, Active 177, Recovery 88, Cool down 27); 361
  steps across 319 blocks, so 42 steps share a role with the previous step.
- 94 intent-note lines (6 verbatim forms, matched case-insensitively) plus 2
  free-text session descriptions (E2 on its own line, E4 on the header line).
- Everything unparseable or ambiguous must surface in ``corpus.issues`` —
  never be silently dropped.

The synthetic-issue tests at the bottom use minimal invented inputs and are
marked as such; everything else is real corpus content.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.agent.workout_library import (
    ZONE_VOCABULARY,
    BlockRole,
    BpmTarget,
    Cadence,
    Note,
    Repetition,
    RpeTarget,
    Step,
    parse_cycling_corpus,
)

CORPUS_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "exercises"
    / "EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt"
)

# --- Real excerpts (verbatim, LF-normalized) --------------------------------

# File lines 1-31: title + the whole E1 session.
EXCERPT_TITLE_E1 = """\
EJEMPLO DE ENTRENAMIENTO PARA CICLISMO:

E1:
Warm up

30 min @ 67 bpm
Zone 1: Recovery
Active

15 min @ 127 bpm
Zone 2: Aerobic
Recovery

5 min @ 81 bpm
Zone 1: Recovery
Active

15 min @ 127 bpm
Zone 2: Aerobic
Recovery

5 min @ 81 bpm
Zone 1: Recovery
Active

15 min @ 127 bpm
Zone 2: Aerobic
Cool down

40 min @ 67 bpm
Zone 1: Recovery
"""

# File lines 36-68: E3, with two `Repetir 1 veces` markers.
EXCERPT_E3_REPEATS = """\
E3:
Warm up

30 min @ 67 bpm
Zone 1: Recovery
Repetir 1 veces

Active

20 min @ 118 bpm
Zone 1: Recovery
Active

10 min @ 133 bpm
Zone 3: Tempo
Recovery

15 min @ 81 bpm
Zone 1: Recovery
Repetir 1 veces

Active

10 min @ 139 bpm
Zone 4: SubThreshold
Active

4 min @ 126 bpm
Zone 2: Aerobic
Cool down

40 min @ 67 bpm
Zone 1: Recovery
"""

# File lines 33-34: E2's free-text description on the line after the header.
EXCERPT_E2_FREE_TEXT = """\
E2:
RODAJE SUAVE: SOLO DARSE UN PASEO. NADA DE FORZAR, NO PASAR DE Z2 (2h)
"""

# File line 70: E4's free-text description on the header line itself
# (no space after the label colon, and the description contains its own
# colon; the trailing "(3h o más)" has the accent as in the file).
EXCERPT_E4_SAME_LINE = "E4:SALIDA LIBRE:  A PODER SER EN GRUPO. PUEDES FORZAR (3h o más)\n"

# File line 72: E5 header with a trailing space and NO description.
EXCERPT_E5_TRAILING_SPACE = """\
E5:
Warm up

30 min @ 67 bpm
Zone 1: Recovery
"""

# File lines 838-899: a full RPE-only session (no zone labels at all).
EXCERPT_RPE_SESSION = """\
Warm up

20 min @ 1 RPE
Repetir 5 veces

Active

1 min @ 7 RPE
60-60 rpm
Active

1 min @ 7 RPE
90-90 rpm
Recovery

7 min @ 2 RPE
Repetir 5 veces

Active

1 min @ 7 RPE
60-60 rpm
Active

1 min @ 7 RPE
90-90 rpm
Recovery

12 min @ 2 RPE
Repetir 6 veces

A TOPE

15 sec @ 10 RPE
Active

45 sec @ 6 RPE
Recovery

5 min @ 2 RPE
Repetir 6 veces

A TOPE

15 sec @ 10 RPE
Active

45 sec @ 6 RPE
Recovery

5 min @ 2 RPE
Repetir 6 veces

A TOPE

15 sec @ 10 RPE
Active

45 sec @ 6 RPE
Cool down

30:05 @ 1 RPE
"""

# File lines 719-731: Active block with intent notes between steps.
EXCERPT_INTENT_NOTES = """\
Active

4 min @ 136 bpm
Zone 3: Tempo
APRIETA

40 sec @ 148 bpm
Zone 5A: SuperThreshold
NO TIENES QUE LLEGAR A ESTE PULSO
A TOPE

20 sec @ 155 bpm
Zone 5B: Aerobic Capacity
"""

# File lines 1337-1348: the Warm up block context is included so the `a tope`
# and `aceleración` notes sit inside it exactly as in the file.
EXCERPT_LOWERCASE_ACCENTS = """\
Warm up

20 min @ 67 bpm
Zone 1: Recovery
Repetir 1 veces

a tope

20 sec @ 222 bpm
Zone 5C: Anaerobic Capacity
aceleración
Active

40 sec @ 133 bpm
Zone 3: Tempo
aceleración
"""

# File lines 408-416: space-separated cadence `70-70 rpm`.
EXCERPT_CADENCE_RANGE = """\
Active

10 min @ 127 bpm
Zone 2: Aerobic
70-70 rpm
Recovery

6 min @ 81 bpm
Zone 1: Recovery
"""

# File lines 1437-1445: compact cadence `90rpm`.
EXCERPT_CADENCE_COMPACT = """\
Active

5 min @ 141 bpm
Zone 4: SubThreshold
90rpm
Recovery

3 min @ 81 bpm
Zone 1: Recovery
"""

# File lines 449-451: mm:ss duration inside a cool down.
EXCERPT_MMSS = """\
Cool down

31:55 @ 67 bpm
Zone 1: Recovery
"""

# Two regions plus a trailing delimiter (shape of the real corpus regions).
EXCERPT_TWO_REGIONS = """\
E5:
Warm up

30 min @ 67 bpm
Zone 1: Recovery

--

Warm up

20 min @ 81 bpm
Zone 1: Recovery
--
"""


# --- WLK-1: structure tests on real excerpts --------------------------------


def test_title_and_labelled_session_e1() -> None:
    corpus = parse_cycling_corpus(EXCERPT_TITLE_E1)

    assert corpus.title == "EJEMPLO DE ENTRENAMIENTO PARA CICLISMO:"
    assert corpus.issues == ()
    assert len(corpus.sessions) == 1
    session = corpus.sessions[0]
    assert session.label == "E1"
    assert session.description is None
    assert session.line == 3  # the "E1:" header line within the excerpt
    assert [b.role for b in session.blocks] == [
        BlockRole.WARM_UP,
        BlockRole.ACTIVE,
        BlockRole.RECOVERY,
        BlockRole.ACTIVE,
        BlockRole.RECOVERY,
        BlockRole.ACTIVE,
        BlockRole.COOL_DOWN,
    ]
    warm_up = session.blocks[0]
    assert warm_up.line == 4
    assert len(warm_up.items) == 1
    step = warm_up.items[0]
    assert isinstance(step, Step)
    assert step.line == 6
    assert step.duration.raw == "30 min"
    assert step.duration.seconds == 1800
    assert step.target == BpmTarget(bpm=67)
    assert step.zone == "Zone 1: Recovery"
    assert step.cadence is None
    cool_down_step = session.blocks[6].items[0]
    assert isinstance(cool_down_step, Step)
    assert cool_down_step.duration.raw == "40 min"
    assert cool_down_step.target == BpmTarget(bpm=67)


def test_rpe_only_session_has_no_zone_labels() -> None:
    corpus = parse_cycling_corpus(EXCERPT_RPE_SESSION)

    assert corpus.title is None
    assert corpus.issues == ()
    assert len(corpus.sessions) == 1
    session = corpus.sessions[0]
    assert session.label is None
    assert session.line == 1  # starts at "Warm up", no label header

    steps = [
        i for b in session.blocks for i in b.items if isinstance(i, Step)
    ]
    assert len(steps) == 16
    assert all(isinstance(s.target, RpeTarget) for s in steps)
    assert all(s.zone is None for s in steps)

    warm_up_items = session.blocks[0].items
    assert len(warm_up_items) == 2  # one step + one repetition marker
    first_item = warm_up_items[0]
    assert isinstance(first_item, Step)
    assert first_item.target == RpeTarget(rpe=1)
    repetition = warm_up_items[1]
    assert isinstance(repetition, Repetition)
    assert repetition.times == 5  # captured, not expanded

    cool_down_step = session.blocks[-1].items[0]
    assert isinstance(cool_down_step, Step)
    assert cool_down_step.duration.raw == "30:05"
    assert cool_down_step.duration.seconds == 1805  # 30 min 5 sec
    assert cool_down_step.target == RpeTarget(rpe=1)
    assert cool_down_step.line == 62  # excerpt line of "30:05 @ 1 RPE"


def test_repetition_markers_captured_never_expanded() -> None:
    corpus = parse_cycling_corpus(EXCERPT_E3_REPEATS)

    assert corpus.issues == ()
    session = corpus.sessions[0]
    assert session.label == "E3"
    steps = [
        i for b in session.blocks for i in b.items if isinstance(i, Step)
    ]
    repetitions = [
        i
        for b in session.blocks
        for i in b.items
        if isinstance(i, Repetition)
    ]
    # 7 step lines in the source; the two `Repetir 1 veces` markers must NOT
    # multiply them.
    assert len(steps) == 7
    assert len(repetitions) == 2
    assert all(r.times == 1 for r in repetitions)
    warm_up_items = session.blocks[0].items
    assert len(warm_up_items) == 2  # step, then the marker that follows it
    marker = warm_up_items[1]
    assert isinstance(marker, Repetition)
    assert marker.line == 6  # excerpt line of "Repetir 1 veces"


def test_free_text_labelled_sessions() -> None:
    e2 = parse_cycling_corpus(EXCERPT_E2_FREE_TEXT)
    assert e2.issues == ()
    assert len(e2.sessions) == 1
    assert e2.sessions[0].label == "E2"
    assert e2.sessions[0].description == (
        "RODAJE SUAVE: SOLO DARSE UN PASEO. NADA DE FORZAR, NO PASAR DE Z2 (2h)"
    )
    assert e2.sessions[0].blocks == ()

    e4 = parse_cycling_corpus(EXCERPT_E4_SAME_LINE)
    assert e4.issues == ()
    assert e4.sessions[0].label == "E4"
    assert e4.sessions[0].description == (
        "SALIDA LIBRE:  A PODER SER EN GRUPO. PUEDES FORZAR (3h o más)"
    )
    assert e4.sessions[0].blocks == ()

    e5 = parse_cycling_corpus(EXCERPT_E5_TRAILING_SPACE)
    assert e5.issues == ()
    assert e5.sessions[0].label == "E5"
    assert e5.sessions[0].description is None
    assert [b.role for b in e5.sessions[0].blocks] == [BlockRole.WARM_UP]
    e5_step = e5.sessions[0].blocks[0].items[0]
    assert isinstance(e5_step, Step)
    assert e5_step.target == BpmTarget(bpm=67)


def test_two_regions_and_trailing_delimiter() -> None:
    corpus = parse_cycling_corpus(EXCERPT_TWO_REGIONS)

    assert corpus.issues == ()
    assert len(corpus.sessions) == 2  # trailing `--` adds no phantom session
    first, second = corpus.sessions
    assert first.label == "E5"
    assert second.label is None
    assert [b.role for b in second.blocks] == [BlockRole.WARM_UP]
    assert second.line == 9  # "Warm up" line of the second region


def test_zone_labels_verbatim_including_5b_full_name() -> None:
    # The known trap: a truncated extraction turned "Zone 5B: Aerobic
    # Capacity" into "Zone 5B: Aerobic". The vocabulary must be exact.
    assert frozenset(
        {
            "Zone 1: Recovery",
            "Zone 2: Aerobic",
            "Zone 3: Tempo",
            "Zone 4: SubThreshold",
            "Zone 5A: SuperThreshold",
            "Zone 5B: Aerobic Capacity",
            "Zone 5C: Anaerobic Capacity",
        }
    ) == ZONE_VOCABULARY

    corpus = parse_cycling_corpus(EXCERPT_INTENT_NOTES)
    session = corpus.sessions[0]
    zones = [
        i.zone
        for b in session.blocks
        for i in b.items
        if isinstance(i, Step)
    ]
    assert zones == ["Zone 3: Tempo", "Zone 5A: SuperThreshold", "Zone 5B: Aerobic Capacity"]


def test_cadence_range_and_compact_variants() -> None:
    ranged = parse_cycling_corpus(EXCERPT_CADENCE_RANGE)
    step = ranged.sessions[0].blocks[0].items[0]
    assert isinstance(step, Step)
    assert step.cadence == Cadence(rpm_low=70, rpm_high=70, raw="70-70 rpm")
    assert ranged.issues == ()

    compact = parse_cycling_corpus(EXCERPT_CADENCE_COMPACT)
    step = compact.sessions[0].blocks[0].items[0]
    assert isinstance(step, Step)
    assert step.cadence == Cadence(rpm_low=90, rpm_high=90, raw="90rpm")
    assert compact.issues == ()


def test_intent_notes_captured_case_insensitive_with_accents() -> None:
    corpus = parse_cycling_corpus(EXCERPT_INTENT_NOTES)
    session = corpus.sessions[0]
    notes = [
        i
        for b in session.blocks
        for i in b.items
        if isinstance(i, Note)
    ]
    assert [(n.text, n.is_intent, n.line) for n in notes] == [
        ("APRIETA", True, 5),
        ("NO TIENES QUE LLEGAR A ESTE PULSO", True, 9),
        ("A TOPE", True, 10),
    ]

    lower = parse_cycling_corpus(EXCERPT_LOWERCASE_ACCENTS)
    assert lower.issues == ()
    notes = [
        i
        for b in lower.sessions[0].blocks
        for i in b.items
        if isinstance(i, Note)
    ]
    assert [(n.text, n.is_intent) for n in notes] == [
        ("a tope", True),
        ("aceleración", True),  # accent preserved verbatim
        ("aceleración", True),
    ]
    assert [n.line for n in notes] == [7, 11, 16]
    assert lower.issues == ()


def test_mmss_duration() -> None:
    corpus = parse_cycling_corpus(EXCERPT_MMSS)
    step = corpus.sessions[0].blocks[0].items[0]
    assert isinstance(step, Step)
    assert step.duration.raw == "31:55"
    assert step.duration.seconds == 1915


def test_sec_duration() -> None:
    corpus = parse_cycling_corpus(EXCERPT_INTENT_NOTES)
    steps = [
        i
        for b in corpus.sessions[0].blocks
        for i in b.items
        if isinstance(i, Step)
    ]
    assert steps[1].duration.raw == "40 sec"
    assert steps[1].duration.seconds == 40
    assert steps[2].duration.raw == "20 sec"
    assert steps[2].duration.seconds == 20


# --- Unparseable / ambiguous content must surface, never vanish --------------
# The excerpts below are SYNTHETIC minimal inputs (clearly not corpus text).


def test_step_before_any_block_is_surfaced() -> None:
    corpus = parse_cycling_corpus("30 min @ 67 bpm\n")

    assert corpus.sessions[0].blocks == ()  # not silently attached anywhere
    assert len(corpus.issues) == 1
    issue = corpus.issues[0]
    assert issue.line == 1
    assert issue.text == "30 min @ 67 bpm"
    assert "before any block" in issue.reason


def test_zone_without_preceding_step_is_surfaced() -> None:
    corpus = parse_cycling_corpus("Warm up\nZone 1: Recovery\n")

    assert corpus.sessions[0].blocks[0].items == ()
    assert len(corpus.issues) == 1
    assert corpus.issues[0].line == 2
    assert corpus.issues[0].text == "Zone 1: Recovery"


def test_repetition_without_block_is_surfaced() -> None:
    corpus = parse_cycling_corpus("Repetir 3 veces\n")

    assert corpus.issues != ()
    assert corpus.issues[0].text == "Repetir 3 veces"


def test_unknown_zone_label_is_kept_verbatim_and_surfaced() -> None:
    corpus = parse_cycling_corpus("Warm up\n\n30 min @ 67 bpm\nZone 9: Mystery\n")

    step = corpus.sessions[0].blocks[0].items[0]
    assert isinstance(step, Step)
    assert step.zone == "Zone 9: Mystery"  # verbatim, not dropped
    assert len(corpus.issues) == 1
    assert corpus.issues[0].text == "Zone 9: Mystery"
    assert "unknown zone label" in corpus.issues[0].reason


def test_second_zone_for_same_step_is_surfaced_first_wins() -> None:
    corpus = parse_cycling_corpus(
        "Warm up\n\n30 min @ 67 bpm\nZone 1: Recovery\nZone 2: Aerobic\n"
    )

    step = corpus.sessions[0].blocks[0].items[0]
    assert isinstance(step, Step)
    assert step.zone == "Zone 1: Recovery"  # first attachment wins
    assert len(corpus.issues) == 1
    assert corpus.issues[0].line == 5
    assert corpus.issues[0].text == "Zone 2: Aerobic"


def test_unrecognized_line_is_captured_as_note_and_surfaced() -> None:
    corpus = parse_cycling_corpus(
        "Warm up\n\n30 min @ 67 bpm\nZone 1: Recovery\ngibberish line\n"
    )

    items = corpus.sessions[0].blocks[0].items
    assert len(items) == 2
    note = items[1]
    assert isinstance(note, Note)
    assert note.text == "gibberish line"
    assert note.is_intent is False
    assert len(corpus.issues) == 1
    assert corpus.issues[0].line == 5
    assert corpus.issues[0].text == "gibberish line"


# --- Full-corpus accounting: the anti-silent-loss regression -----------------


@pytest.mark.skipif(not CORPUS_PATH.exists(), reason="corpus file not present")
def test_full_corpus_accounting() -> None:
    """Re-measured grammar of THIS file; every number hand-verified.

    Hypothesis differences found during measurement: the file uses CRLF line
    endings; 33 steps use mm:ss durations; the intent vocabulary has 6 forms
    (`a tope` lowercase in addition to `A TOPE`); the file ends with a
    trailing `--` delimiter.
    """
    text = CORPUS_PATH.read_text(encoding="utf-8")
    corpus = parse_cycling_corpus(text)

    assert corpus.title == "EJEMPLO DE ENTRENAMIENTO PARA CICLISMO:"
    assert corpus.issues == (), "the parser must account for every line"

    assert len(corpus.sessions) == 29  # 5 labelled + 24 unlabelled
    assert [s.label for s in corpus.sessions[:5]] == ["E1", "E2", "E3", "E4", "E5"]
    assert all(s.label is None for s in corpus.sessions[5:])

    by_label = {s.label: s for s in corpus.sessions[:5]}
    assert by_label["E1"].blocks and by_label["E2"].description
    assert by_label["E2"].blocks == ()
    assert by_label["E4"].description and by_label["E4"].blocks == ()
    assert by_label["E5"].blocks

    all_items = [
        i for s in corpus.sessions for b in s.blocks for i in b.items
    ]
    steps = [i for i in all_items if isinstance(i, Step)]
    assert len(steps) == 361
    bpm = [s for s in steps if isinstance(s.target, BpmTarget)]
    rpe = [s for s in steps if isinstance(s.target, RpeTarget)]
    assert len(bpm) == 329
    assert len(rpe) == 32

    zones = [s.zone for s in bpm]
    assert all(z is not None for z in zones)
    assert Counter(zones) == Counter(
        {
            "Zone 1: Recovery": 140,
            "Zone 2: Aerobic": 73,
            "Zone 3: Tempo": 32,
            "Zone 4: SubThreshold": 20,
            "Zone 5A: SuperThreshold": 24,
            "Zone 5B: Aerobic Capacity": 24,
            "Zone 5C: Anaerobic Capacity": 16,
        }
    )

    cadences = [s.cadence for s in steps if s.cadence is not None]
    assert len(cadences) == 49
    assert {c.raw for c in cadences} == {
        "50-50 rpm",
        "50rpm",
        "60-60 rpm",
        "70-70 rpm",
        "80-80 rpm",
        "80rpm",
        "90-90 rpm",
        "90rpm",
    }

    repetitions = [i for i in all_items if isinstance(i, Repetition)]
    assert len(repetitions) == 80
    assert Counter(r.times for r in repetitions) == Counter(
        {1: 42, 2: 4, 3: 6, 4: 2, 5: 14, 6: 12}
    )

    notes = [i for i in all_items if isinstance(i, Note)]
    assert len(notes) == 94
    assert all(n.is_intent for n in notes)

    blocks = [b for s in corpus.sessions for b in s.blocks]
    assert len(blocks) == 319
    assert Counter(b.role for b in blocks) == Counter(
        {
            BlockRole.WARM_UP: 27,
            BlockRole.ACTIVE: 177,
            BlockRole.RECOVERY: 88,
            BlockRole.COOL_DOWN: 27,
        }
    )

    # Provenance: every parsed node points at a plausible source line.
    assert corpus.sessions[0].line == 3
    first_step = corpus.sessions[0].blocks[0].items[0]
    assert isinstance(first_step, Step)
    assert first_step.line == 6
    assert all(0 < s.line <= text.count("\n") + 1 for s in corpus.sessions)
