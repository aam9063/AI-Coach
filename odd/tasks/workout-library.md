# ODD Feature: workout-library

Status: complete (merged PR #18) | Source: owner request (data bank in docs/exercises)

## Objective

Turn the owner's reference workout files in `docs/exercises/` into a structured, typed, tested data bank that AI-Coach can use to ground training advice and, later, deterministic session generation (PROJECT_BRIEF.md defers full plan generation to a later feature; v1 uses it as agent reference material).

## Problem

The owner keeps three reference sources: two TXT files (cycling sessions in exported-workout format; gym-for-cycling sessions) and one PDF guide (8-week plan, Pedalea con Ciencia). Today they are prose only: nothing in the codebase can read, validate, or cite them. The agent must never invent numbers (PROJECT_BRIEF.md), so reference workouts must be structured data, not prompt prose.

## Scope

- `backend/app/agent/workout_library.py`: typed, frozen dataclasses and pure parsing functions:
  - Cycling sessions from `docs/exercises/EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt`: named sessions (E1..E5 + unnamed), warm-up/cool-down and Active/Recovery blocks with `duration @ bpm` or `@ RPE`, zone labels (Zone 1 Recovery, Zone 2 Aerobic, Zone 3 Tempo, Zone 4 SubThreshold, Zone 5A SuperThreshold, Zone 5B Aerobic Capacity, Zone 5C Anaerobic Capacity), optional cadence (rpm) lines, free-text notes (e.g. "APRIETA", "A TOPE", "NO TIENES QUE LLEGAR A ESTE PULSO"), and `Repetir N veces` repetition groups, sessions separated by `--`.
  - Gym sessions from `docs/exercises/gym.txt`: focus sections (TREN INFERIOR / TREN SUPERIOR / CORE), warm-up and activation entries, exercises with sets x rep schemes, RIR, rest times, tempo notes (e.g. 4-second eccentric), core holds.
  - A loader that reads the canonical JSON fixtures and returns the library (the parser itself stays pure text->data).
- `backend/data/exercises/`: canonical JSON fixtures generated from the TXT sources by a committed script (`backend/scripts/generate_workout_fixtures.py`), so regeneration is reproducible and diff-able.
- Tests: parser unit tests (real TXT content as fixtures in test), round-trip/regression tests asserting the committed JSON matches a fresh parse, and loader tests.

## Constraints

- Pure parsing: no I/O inside parse functions; I/O only in the loader and the generation script (mirrors engine purity conventions, PROJECT_BRIEF.md §6).
- Fully typed (mypy strict), ruff clean, `uv run pytest` green; test-first where a meaningful RED exists.
- Zone vocabulary matches the source files exactly (5A/5B/5C naming as in the TXT, mapped conceptually to the engine's Friel Z5a/b/c but stored verbatim).
- Unparseable or ambiguous lines are surfaced explicitly (warnings in the parse result), never silently dropped.
- The PDF is NOT parsed into this data bank; it stays reference material for the evidence store (decision recorded below).
- No changes to files belonging to the in-progress `feat/whatsapp-prompt` work (uncommitted at branch time).
- English for all code artifacts; Spanish workout text is preserved verbatim inside data.

## Checklist

- [x] `WLK-1`: RED: parser tests for cycling TXT structure (sessions, blocks, zones, repeats, cadence, RPE variants, notes) against real excerpts.
- [x] `WLK-2`: GREEN: implement cycling parser in `backend/app/agent/workout_library.py`.
- [x] `WLK-3`: RED+GREEN: gym parser tests and implementation (focus sections, sets x reps, RIR, rest, tempo, activation, core holds).
- [x] `WLK-4`: Fixture generation script + canonical JSON in `backend/data/exercises/` + round-trip regression test (committed JSON == fresh parse).
- [x] `WLK-5`: Loader API returning a typed `WorkoutLibrary` with lookup by id/name, plus module docstring documenting provenance and the later generator seam.
- [x] `WLK-6`: Quality gates (ruff, mypy strict, pytest) and work-unit commit with evidence recorded here.

## Acceptance criteria

- Both TXT sources parse fully; a regression test proves the committed fixtures reproduce exactly from the sources (no drift, no silent loss).
- Parse results carry per-line provenance sufficient to trace any block back to the source text.
- All project quality gates pass; no interference with `feat/whatsapp-prompt` files.

## Verification evidence

(to be filled per task)

## Progress

**WLK-1/WLK-2 done at `e3f0c93`**: the cycling parser is pure (no I/O), typed and frozen, with absolute 1-based source-line provenance on every node, zone labels stored VERBATIM, `Repetir` markers captured but never expanded, and intent notes as positional `Note` items (their attachment to the preceding or following step is genuinely ambiguous in the source, so it is preserved structurally and deliberately not guessed). **The accounting is the claim that matters**: all 1264 non-blank lines classify, `issues == ()` for the whole corpus, and anything the grammar does not know is BOTH captured verbatim AND surfaced as a `ParseIssue`.

**The grammar was re-measured against THIS file instead of copied**, and it differs from the earlier measurement of the same-named file in another project in five ways: the file is **CRLF**; 33 steps use `mm:ss` durations; there are **six** verbatim intent forms (lowercase `a tope` x12 alongside `A TOPE` x18); **`E1`..`E5` live INSIDE the first `--` region** (only the 24 unlabelled sessions are delimiter-separated, and the file ends with a trailing delimiter); and the session headers have quirks (`E4:SALIDA LIBRE:` with no space, `E5: ` with a trailing space, E2's description on the next line). The structural difference alone would have broken a parser built on the hypothesis. Independently re-measured by the parent: `--`x25, `Repetir`x80, `@ bpm`x329, `@ RPE`x32, `rpm`x49, `Warm up`x27, CR on all 1727 lines, and the seven zone labels with their exact counts (Z1x140, Z2x73, Z3x32, Z4x20, Z5Ax24, Z5Bx24, Z5Cx16) -- every number matching. 17 tests: twelve on real verbatim excerpts, six on synthetic issue cases, and one full-corpus accounting regression.

**WLK-3/WLK-4/WLK-5 done at `f2b3bbd`** (and WLK-6 closed with them: gates green, work-unit commits, evidence recorded here). The gym parser follows the cycling conventions exactly -- pure, frozen, absolute line provenance, nothing dropped silently: 37 of 39 content lines classify and **exactly two** carry digit-bearing prose the grammar deliberately does not model, both captured verbatim AND surfaced as a ParseIssue and pinned by the round-trip fixture. The hypothesis was re-measured and differed **five** times, twice structurally: the CORE block is **not** entirely prose (five parseable holds with curly quotes and per-leg rep notation), and an ACTIVACION sub-block inside TREN INFERIOR is **delimited by a blank line** -- a structure whose absence would have merged two different training blocks. RIR held at three exercises (optional in the model), the Spanish rest notation held, and this corpus has no trailing newline and no trailing delimiter, unlike the cycling one.

**One deliberate repository change**: `.gitignore` ignored `backend/data/` as a DIRECTORY, and git cannot re-include a file whose parent directory is excluded -- so the curated fixtures could never be committed there. It now ignores the directory's CONTENT and re-includes `backend/data/exercises/`, with the reason written where the rule lives; the parent verified both directions (the two fixtures are visible to git, and a stray file under `backend/data/` is still ignored).

**WLK-6 evidence**: the three workout-library test files 30 passed; ruff clean; mypy strict clean (176 files); the parent re-verified the anti-drift claim independently by regenerating the fixtures and comparing checksums (both OK); and `git diff --name-only origin/dev...HEAD` lists only .gitignore, backend/app/agent, backend/data/exercises, backend/scripts and backend/tests/agent -- so the "no interference" criterion holds by construction.

Owner decisions recorded: parser lives in `backend/app/agent/` (agent reference material, not engine science); PDF excluded from scope (evidence-store material); fixtures are generated artifacts committed to git for reviewability.
