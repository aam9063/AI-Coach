# Verbatim corpus excerpts: real source lines kept byte-exact (Spanish prime
# U+00B4, curly U+201D quotes, and one 114-character source line). The
# ambiguous-character and line-length rules would force edits that break
# that verbatim-ness; suppressed for this file only.
# ruff: noqa: E501, RUF001, RUF002, RUF003

"""Gym corpus parser tests (WLK-3) — real excerpts, measured grammar.

Every excerpt below is copied VERBATIM from ``docs/exercises/gym.txt``
(LF-normalized for readability; the real file has CRLF endings and NO
trailing newline, which the full-corpus test exercises). The grammar was
re-measured against THIS file by hand; it differs from the earlier
project's hypothesis ("3 focus blocks, 19 exercises, 10 prose items, RIR
on 3 exercises, Spanish rest notation, CORE entirely prose") in these
measured ways:

- 3 focus sections (TREN INFERIOR / TREN SUPERIOR / CORE) — hypothesis held,
  but there is a FOURTH header, ``ACTIVACIÓN:``, as a sub-block inside
  TREN INFERIOR that the hypothesis never mentions.
- 18 sets-x-reps exercises + 1 activation prescription that has NO sets
  scheme (``CAMINATA LATERAL ...: 10 PASOS A CADA DIRECCIÓN.``) — the
  hypothesis's "19 exercises" only works if that 19th entry counted, and
  it cannot parse as an exercise (surfaced as an issue, never dropped).
- 4 prose lines (not 10): warm-up note of TREN INFERIOR, mobility note of
  TREN SUPERIOR, ``SIN CALENTAMIENTO.`` of CORE, plus the unparsed
  activation prescription.
- RIR on exactly 3 exercises (hypothesis held); Spanish rest notation on
  3 rest lines (hypothesis held).
- CORE is NOT entirely prose: 1 prose line + 5 parseable hold entries
  (planks with second holds, bridges with reps/pierna).
- Structural details the hypothesis never mentions: CRLF endings; NO
  trailing ``--`` delimiter (the cycling corpus ends with one); ``”``
  (U+201D) for CORE second-holds vs ``´´`` (U+00B4 twice) in TREN
  sections; compact ``10reps/pierna``; a scheme-first exercise
  (``3x25 crunch abdomen``); uppercase ``3X12`` X; ``Crunch de
  abdomen:4x25-30.`` with no space after the colon; ``-Press banca-->
  4x13,`` decoration; the ``Pres banca inclinado`` typo; a trailing comma
  after a scheme; a rest line with a trailing period (``2´ DE REC.``).

The synthetic-issue tests at the bottom use minimal invented inputs and
are marked as such; everything else is real corpus content.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent.workout_library import (
    Duration,
    GymExercise,
    GymHold,
    GymProse,
    GymRest,
    parse_gym_corpus,
)

GYM_CORPUS_PATH = Path(__file__).resolve().parents[3] / "docs" / "exercises" / "gym.txt"

# --- Real excerpts (verbatim, LF-normalized) --------------------------------

# File lines 1-26: title + the whole TREN INFERIOR region.
EXCERPT_TREN_INFERIOR = """\
EJEMPLO ENTRENAMIENTO GYM PARA CICLISMO:

- TREN INFERIOR:
  10´ de calentamiento en elíptica, comba, bici, etc.

ACTIVACIÓN:
CLAMSHELL GLUTEO: 3X12 A CADA LADO.
CAMINATA LATERAL CON BANDA ELÁSTICA: 10 PASOS A CADA DIRECCIÓN.
ABDUCCIÓN DE CADERA CON BANDA ELÁSTICA: 3X15.

Prensa: 5x20-15-15-10-10 (la primera de calentamiento). RIR 5
1´ 30´´ REC

Extensión de isquios: 5x12-12-10-10. ( LAS DOS PRIMERAS A RITMO NORMAL, LAS DOS ÚLTIMAS BAJAS DENTO (4 SEG). RIR 5
2´ DE REC

Subida al cajón: 4x15-10-8-8. RIR 4
2´ DE REC.

Hip thrust: 3x8/10.

Gemelo: 4x14.

Crunch de abdomen:4x25-30.
3 planchas de 1 minuto.
"""

# File lines 29-46: the whole TREN SUPERIOR region (with the real title
# prepended, as the grammar expects it before any header).
EXCERPT_TREN_SUPERIOR = """\
EJEMPLO ENTRENAMIENTO GYM PARA CICLISMO:

- TREN SUPERIOR:
Puedes hacer movilidad articular de hombros.

-Press banca--> 4x13,
-Pres banca inclinado--> 3x12-10-8.

Jalón al pecho: 4x12-10-8.
Remo con barra: 4x9.
Remo polea baja: 4x10-10-8-8

Curl de biceps: 4x10-10-8-8.
Martillo biceps: 4x10-8-8-8.

Extensión triceps con cuerda: 3x10/12
Press francés: 4x7.

3x25 crunch abdomen
3 planchas de 30´´
"""

# File lines 50-61: the whole CORE region (no trailing delimiter in the file).
EXCERPT_CORE = """\
EJEMPLO ENTRENAMIENTO GYM PARA CICLISMO:

CORE:
SIN CALENTAMIENTO.

PLANCHA FRONTAL 2 APOYOS (codo y pie contrario) 15” cada lado

PUENTE DE PIERNAS FITBALL A UNA PIERNA 10 reps/pierna

APERTURAS PIERNA LATERAL TUMBADO 15 reps/pierna

PUENTE DE CADERA UN APOYO ARRIBA Y ABAJO 10reps/pierna

PLANCHA LATERAL A UNA PIERNA 15”/lado
"""


def _exercises(section: object) -> list[GymExercise]:
    from app.agent.workout_library import GymActivationBlock, GymSection

    assert isinstance(section, GymSection)
    found: list[GymExercise] = []
    for item in section.items:
        if isinstance(item, GymExercise):
            found.append(item)
        elif isinstance(item, GymActivationBlock):
            found.extend(i for i in item.items if isinstance(i, GymExercise))
    return found


# --- WLK-3: structure tests on real excerpts --------------------------------


def test_title_and_tren_inferior_section() -> None:
    corpus = parse_gym_corpus(EXCERPT_TREN_INFERIOR)

    assert corpus.title == "EJEMPLO ENTRENAMIENTO GYM PARA CICLISMO:"
    assert len(corpus.sections) == 1
    section = corpus.sections[0]
    assert section.name == "TREN INFERIOR"
    assert section.line == 3
    # prose, activation block, then 6 exercises interleaved with 3 rests
    # and closed by a hold — 12 items in source order.
    assert len(section.items) == 12

    prose = section.items[0]
    assert isinstance(prose, GymProse)
    assert prose.text == "10´ de calentamiento en elíptica, comba, bici, etc."
    assert prose.line == 4

    from app.agent.workout_library import GymActivationBlock

    activation = section.items[1]
    assert isinstance(activation, GymActivationBlock)
    assert activation.name == "ACTIVACIÓN"
    assert activation.line == 6
    assert len(activation.items) == 3
    clamshell = activation.items[0]
    assert isinstance(clamshell, GymExercise)
    assert clamshell.name == "CLAMSHELL GLUTEO"
    assert clamshell.sets == 3
    assert clamshell.reps_raw == "12"  # uppercase 3X12: X case not stored
    assert clamshell.reps == (12,)
    assert clamshell.rir is None  # RIR is OPTIONAL; activation states none
    assert clamshell.note == "A CADA LADO"
    assert clamshell.line == 7
    # The 10-PASOS prescription has no sets scheme: captured verbatim AND
    # surfaced as an issue (see test below).
    caminata = activation.items[1]
    assert isinstance(caminata, GymProse)
    assert caminata.line == 8
    abduccion = activation.items[2]
    assert isinstance(abduccion, GymExercise)
    assert abduccion.name == "ABDUCCIÓN DE CADERA CON BANDA ELÁSTICA"
    assert abduccion.sets == 3
    assert abduccion.reps == (15,)
    assert abduccion.line == 9

    prensa = section.items[2]
    assert isinstance(prensa, GymExercise)
    assert prensa.name == "Prensa"
    assert prensa.sets == 5
    assert prensa.reps_raw == "20-15-15-10-10"
    assert prensa.reps == (20, 15, 15, 10, 10)
    assert prensa.rir == 5
    assert prensa.note == "(la primera de calentamiento)"
    assert prensa.line == 11

    rest = section.items[3]
    assert isinstance(rest, GymRest)
    assert rest.duration == Duration(seconds=90, raw="1´ 30´´ REC")
    assert rest.line == 12

    extension = section.items[4]
    assert isinstance(extension, GymExercise)
    assert extension.name == "Extensión de isquios"
    assert extension.reps == (12, 12, 10, 10)
    assert extension.rir == 5
    # tempo note preserved verbatim (including the "DENTO" typo and "4 SEG")
    assert extension.note == (
        "( LAS DOS PRIMERAS A RITMO NORMAL, LAS DOS ÚLTIMAS BAJAS DENTO (4 SEG)"
    )
    assert extension.line == 14

    assert section.items[5] == GymRest(
        duration=Duration(seconds=120, raw="2´ DE REC"), line=15
    )
    subida = section.items[6]
    assert isinstance(subida, GymExercise)
    assert subida.name == "Subida al cajón"
    assert subida.reps == (15, 10, 8, 8)
    assert subida.rir == 4
    assert subida.note is None
    assert subida.line == 17
    # trailing period on the rest line is tolerated
    assert section.items[7] == GymRest(
        duration=Duration(seconds=120, raw="2´ DE REC."), line=18
    )
    hip = section.items[8]
    assert isinstance(hip, GymExercise)
    assert hip.name == "Hip thrust"
    assert hip.reps == (8, 10)  # slash-separated alternates, kept as tokens
    assert hip.line == 20
    gemelo = section.items[9]
    assert isinstance(gemelo, GymExercise)
    assert gemelo.name == "Gemelo"
    assert gemelo.reps == (14,)
    crunch = section.items[10]
    assert isinstance(crunch, GymExercise)
    assert crunch.name == "Crunch de abdomen"  # no space after the colon
    assert crunch.reps == (25, 30)  # range-shaped tokens kept verbatim
    assert crunch.line == 24

    hold = section.items[11]
    assert isinstance(hold, GymHold)
    assert hold.name == "planchas"
    assert hold.rounds == 3
    assert hold.seconds == 60
    assert hold.reps is None
    assert hold.per is None
    assert hold.raw == "3 planchas de 1 minuto."
    assert hold.line == 25

    # Exactly two issues in this excerpt: the warm-up line carries a duration
    # (10´) and the activation prescription a step count (10 PASOS) — both
    # quantities the grammar deliberately does not model; captured verbatim
    # AND surfaced.
    assert [(i.line, "digits" in i.reason) for i in corpus.issues] == [
        (4, True),
        (8, True),
    ]
    assert (
        corpus.issues[1].text
        == "CAMINATA LATERAL CON BANDA ELÁSTICA: 10 PASOS A CADA DIRECCIÓN."
    )


def test_tren_superior_section() -> None:
    corpus = parse_gym_corpus(EXCERPT_TREN_SUPERIOR)

    assert corpus.title == "EJEMPLO ENTRENAMIENTO GYM PARA CICLISMO:"
    assert corpus.issues == ()
    assert len(corpus.sections) == 1
    section = corpus.sections[0]
    assert section.name == "TREN SUPERIOR"
    assert section.line == 3

    exercises = _exercises(section)
    assert [(e.name, e.sets, e.reps_raw, e.line) for e in exercises] == [
        ("Press banca", 4, "13", 6),  # "-Press banca-->" decoration stripped
        ("Pres banca inclinado", 3, "12-10-8", 7),  # source typo kept verbatim
        ("Jalón al pecho", 4, "12-10-8", 9),
        ("Remo con barra", 4, "9", 10),
        ("Remo polea baja", 4, "10-10-8-8", 11),  # no trailing period
        ("Curl de biceps", 4, "10-10-8-8", 13),
        ("Martillo biceps", 4, "10-8-8-8", 14),
        ("Extensión triceps con cuerda", 3, "10/12", 16),
        ("Press francés", 4, "7", 17),
        ("crunch abdomen", 3, "25", 19),  # scheme-first form
    ]
    assert all(e.rir is None and e.note is None for e in exercises)
    assert all(
        e.reps == tuple(int(t) for t in re.split(r"[-/]", e.reps_raw))
        for e in exercises
    )

    hold = section.items[-1]
    assert isinstance(hold, GymHold)
    assert hold.name == "planchas"
    assert hold.rounds == 3
    assert hold.seconds == 30
    assert hold.raw == "3 planchas de 30´´"
    assert hold.line == 20


def test_core_section_holds() -> None:
    corpus = parse_gym_corpus(EXCERPT_CORE)

    assert corpus.issues == ()
    assert len(corpus.sections) == 1
    section = corpus.sections[0]
    assert section.name == "CORE"
    assert section.line == 3
    assert len(section.items) == 6  # 1 prose + 5 holds — NOT "entirely prose"

    prose = section.items[0]
    assert isinstance(prose, GymProse)
    assert prose.text == "SIN CALENTAMIENTO."
    assert prose.line == 4

    holds = [i for i in section.items if isinstance(i, GymHold)]
    assert [(h.name, h.seconds, h.reps, h.per, h.line) for h in holds] == [
        ("PLANCHA FRONTAL 2 APOYOS (codo y pie contrario)", 15, None, "cada lado", 6),
        ("PUENTE DE PIERNAS FITBALL A UNA PIERNA", None, 10, "pierna", 8),
        ("APERTURAS PIERNA LATERAL TUMBADO", None, 15, "pierna", 10),
        ("PUENTE DE CADERA UN APOYO ARRIBA Y ABAJO", None, 10, "pierna", 12),
        ("PLANCHA LATERAL A UNA PIERNA", 15, None, "lado", 14),
    ]
    # the compact "10reps/pierna" variant and the curly-quote seconds are
    # all captured with their verbatim lines
    assert holds[3].raw == "PUENTE DE CADERA UN APOYO ARRIBA Y ABAJO 10reps/pierna"
    assert holds[4].raw == "PLANCHA LATERAL A UNA PIERNA 15”/lado"
    assert all(h.rounds is None for h in holds)


# --- Unparseable / ambiguous content must surface, never vanish --------------
# The inputs below are SYNTHETIC minimal cases (clearly not corpus text).


def test_digit_bearing_unmatched_line_is_surfaced() -> None:
    corpus = parse_gym_corpus("CORE:\n10 PASOS DE ALGO RARO.\n")

    section = corpus.sections[0]
    assert isinstance(section.items[0], GymProse)  # captured verbatim...
    assert section.items[0].text == "10 PASOS DE ALGO RARO."
    assert len(corpus.issues) == 1  # ...AND surfaced, never silently dropped
    issue = corpus.issues[0]
    assert issue.line == 2
    assert issue.text == "10 PASOS DE ALGO RARO."
    assert "digits" in issue.reason


def test_prose_without_digits_is_a_recognized_category() -> None:
    corpus = parse_gym_corpus("CORE:\nSIN CALENTAMIENTO.\n")

    assert corpus.issues == ()  # prose is expected content, not an issue
    assert isinstance(corpus.sections[0].items[0], GymProse)


def test_second_activation_header_is_surfaced() -> None:
    corpus = parse_gym_corpus(
        "TITULO:\n"
        "TREN INFERIOR:\nACTIVACIÓN:\nCLAMSHELL: 3X12.\nMOVILIDAD:\n"
    )

    assert len(corpus.issues) == 1
    issue = corpus.issues[0]
    assert issue.line == 5
    assert issue.text == "MOVILIDAD:"
    assert "header" in issue.reason


def test_headerless_region_forms_an_unnamed_section() -> None:
    corpus = parse_gym_corpus("Gemelo: 4x14.\n")

    assert corpus.issues == ()
    assert corpus.sections[0].name is None
    assert corpus.sections[0].line == 1
    exercise = corpus.sections[0].items[0]
    assert isinstance(exercise, GymExercise)
    assert exercise.name == "Gemelo"


# --- Full-corpus accounting: the anti-silent-loss regression -----------------


@pytest.mark.skipif(not GYM_CORPUS_PATH.exists(), reason="corpus file not present")
def test_full_corpus_accounting() -> None:
    """Re-measured grammar of THIS file; every number hand-verified.

    Measured differences from the earlier hypothesis: 18 set-scheme
    exercises (not 19 — the 19th entry, the CAMINATA 10-PASOS activation
    line, has no scheme and surfaces as an issue); 4 prose lines (not 10);
    CORE holds parse as entries (not "entirely prose"); an ACTIVACIÓN
    sub-header the hypothesis never mentions; no trailing ``--``.
    """
    text = GYM_CORPUS_PATH.read_text(encoding="utf-8")
    corpus = parse_gym_corpus(text)

    assert corpus.title == "EJEMPLO ENTRENAMIENTO GYM PARA CICLISMO:"
    assert corpus.issues != ()  # exactly one known unparsed line (see below)

    # 2 `--` delimiters split the corpus into exactly 3 sections; no
    # trailing delimiter, so no phantom section.
    assert [s.name for s in corpus.sections] == [
        "TREN INFERIOR",
        "TREN SUPERIOR",
        "CORE",
    ]
    assert [s.line for s in corpus.sections] == [3, 29, 50]

    from app.agent.workout_library import GymActivationBlock

    all_items = [i for s in corpus.sections for i in s.items]
    exercises: list[GymExercise] = []
    for section in corpus.sections:
        for item in section.items:
            if isinstance(item, GymExercise):
                exercises.append(item)
            elif isinstance(item, GymActivationBlock):
                exercises.extend(i for i in item.items if isinstance(i, GymExercise))
    assert isinstance(all_items[0], GymProse)
    assert len(exercises) == 18  # 2 activation + 6 inferior + 10 superior
    assert [e.rir for e in exercises].count(5) == 2
    assert [e.rir for e in exercises].count(4) == 1
    assert sum(1 for e in exercises if e.rir is not None) == 3  # hypothesis held

    holds = [i for i in all_items if isinstance(i, GymHold)]
    assert len(holds) == 7  # 2 plancha rounds + 5 core holds
    rests = [i for i in all_items if isinstance(i, GymRest)]
    assert len(rests) == 3
    assert [r.duration.seconds for r in rests] == [90, 120, 120]
    proses = [
        i
        for s in corpus.sections
        for i in s.items
        if isinstance(i, GymProse)
    ]
    assert len(proses) == 3  # warm-up, mobility, SIN CALENTAMIENTO

    activations = [i for s in corpus.sections for i in s.items if isinstance(i, GymActivationBlock)]
    assert len(activations) == 1
    assert activations[0].name == "ACTIVACIÓN"
    assert activations[0].line == 6
    assert len(activations[0].items) == 3

    # Exactly two known issues: the warm-up line (10´ ...) and the
    # activation prescription with no sets scheme — both captured verbatim
    # AND surfaced.
    assert len(corpus.issues) == 2
    assert [(i.line, "digits" in i.reason) for i in corpus.issues] == [
        (4, True),
        (8, True),
    ]
    assert (
        corpus.issues[0].text == "10´ de calentamiento en elíptica, comba, bici, etc."
    )
    assert (
        corpus.issues[1].text
        == "CAMINATA LATERAL CON BANDA ELÁSTICA: 10 PASOS A CADA DIRECCIÓN."
    )

    # Provenance: absolute 1-based source lines on every node.
    assert corpus.sections[0].items[2].line == 11  # Prensa
    assert corpus.sections[1].items[1].line == 32  # Press banca
    assert corpus.sections[2].items[-1].line == 61  # last hold, no trailing newline
    assert all(0 < s.line <= text.count("\n") + 1 for s in corpus.sections)
