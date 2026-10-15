# ODD Feature: workout-library

Status: in progress | Source: owner request (data bank in docs/exercises) | Branch: feat/workout-library (from feat/whatsapp-prompt HEAD, unrelated dirty files left untouched)

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

- [ ] `WLK-1`: RED: parser tests for cycling TXT structure (sessions, blocks, zones, repeats, cadence, RPE variants, notes) against real excerpts.
- [ ] `WLK-2`: GREEN: implement cycling parser in `backend/app/agent/workout_library.py`.
- [ ] `WLK-3`: RED+GREEN: gym parser tests and implementation (focus sections, sets x reps, RIR, rest, tempo, activation, core holds).
- [ ] `WLK-4`: Fixture generation script + canonical JSON in `backend/data/exercises/` + round-trip regression test (committed JSON == fresh parse).
- [ ] `WLK-5`: Loader API returning a typed `WorkoutLibrary` with lookup by id/name, plus module docstring documenting provenance and the later generator seam.
- [ ] `WLK-6`: Quality gates (ruff, mypy strict, pytest) and work-unit commit with evidence recorded here.

## Acceptance criteria

- Both TXT sources parse fully; a regression test proves the committed fixtures reproduce exactly from the sources (no drift, no silent loss).
- Parse results carry per-line provenance sufficient to trace any block back to the source text.
- All project quality gates pass; no interference with `feat/whatsapp-prompt` files.

## Verification evidence

(to be filled per task)

## Progress

Feature doc created; implementation not started.

Owner decisions recorded: parser lives in `backend/app/agent/` (agent reference material, not engine science); PDF excluded from scope (evidence-store material); fixtures are generated artifacts committed to git for reviewability.
