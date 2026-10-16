"""Fixture generation, round-trip, and loader tests (WLK-4/WLK-5).

The committed fixtures in ``backend/data/exercises/`` are GENERATED
artifacts (by ``backend/scripts/generate_workout_fixtures.py``) committed
to git on purpose: reviewability. The round-trip regression below turns
drift and silent loss into a test failure instead of a diff nobody reads:

- committed JSON == canonical JSON of a fresh parse of the TXT source;
- committed JSON rebuilds, through the typed dataclasses, into a corpus
  EQUAL to the fresh parse (JSON -> dataclasses -> equality).

The loader tests cover the typed :class:`WorkoutLibrary` API (lookup by
id and by name) against the committed fixtures.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agent.workout_library import (
    CYCLING_FIXTURE_PATH,
    GYM_FIXTURE_PATH,
    Session,
    cycling_corpus_from_dict,
    cycling_corpus_to_dict,
    gym_corpus_from_dict,
    gym_corpus_to_dict,
    load_workout_library,
    parse_cycling_corpus,
    parse_gym_corpus,
    to_canonical_json,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CYCLING_TXT = (
    REPO_ROOT / "docs" / "exercises" / "EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt"
)
GYM_TXT = REPO_ROOT / "docs" / "exercises" / "gym.txt"
GENERATOR_SCRIPT = REPO_ROOT / "backend" / "scripts" / "generate_workout_fixtures.py"


def _load_generator() -> Any:
    """Load the generation script as a module (it is I/O-only; the pure
    projection lives in app.agent.workout_library)."""
    spec = importlib.util.spec_from_file_location(
        "generate_workout_fixtures", GENERATOR_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- WLK-4: committed fixtures == fresh parse (no drift, no silent loss) -----


def test_cycling_fixture_equals_fresh_parse() -> None:
    corpus = parse_cycling_corpus(CYCLING_TXT.read_text(encoding="utf-8"))
    assert corpus.issues == ()  # the whole cycling corpus classifies
    assert len(corpus.sessions) == 29

    committed = CYCLING_FIXTURE_PATH.read_text(encoding="utf-8")
    assert committed == to_canonical_json(cycling_corpus_to_dict(corpus))


def test_gym_fixture_equals_fresh_parse() -> None:
    corpus = parse_gym_corpus(GYM_TXT.read_text(encoding="utf-8"))
    assert [i.line for i in corpus.issues] == [4, 8]  # the two known issues
    assert len(corpus.sections) == 3

    committed = GYM_FIXTURE_PATH.read_text(encoding="utf-8")
    assert committed == to_canonical_json(gym_corpus_to_dict(corpus))


def _assert_round_trip[CorpusT](
    corpus: CorpusT,
    to_dict: Callable[[CorpusT], dict[str, object]],
    from_dict: Callable[[object], CorpusT],
    fixture_path: Path,
) -> None:
    """dict -> dataclasses -> dict is a fixed point, and dataclass equality
    with the fresh parse proves nothing was lost or altered — including via
    the COMMITTED file."""
    rebuilt = from_dict(to_dict(corpus))
    assert rebuilt == corpus
    committed_obj = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert from_dict(committed_obj) == corpus


def test_round_trip_through_dataclasses() -> None:
    _assert_round_trip(
        parse_cycling_corpus(CYCLING_TXT.read_text(encoding="utf-8")),
        cycling_corpus_to_dict,
        cycling_corpus_from_dict,
        CYCLING_FIXTURE_PATH,
    )
    _assert_round_trip(
        parse_gym_corpus(GYM_TXT.read_text(encoding="utf-8")),
        gym_corpus_to_dict,
        gym_corpus_from_dict,
        GYM_FIXTURE_PATH,
    )


def test_generator_reproduces_committed_fixtures() -> None:
    generator = _load_generator()
    outputs = generator.generate_fixtures(
        CYCLING_TXT.read_text(encoding="utf-8"), GYM_TXT.read_text(encoding="utf-8")
    )
    assert outputs["cycling.json"] == CYCLING_FIXTURE_PATH.read_text(encoding="utf-8")
    assert outputs["gym.json"] == GYM_FIXTURE_PATH.read_text(encoding="utf-8")


# --- WLK-5: typed loader with lookup by id and by name -----------------------


def test_loader_returns_typed_library_from_committed_fixtures() -> None:
    library = load_workout_library()

    # 29 cycling sessions (5 labelled + 24 unlabelled) + 3 gym sections.
    assert len(library.entries) == 32
    assert [e.id for e in library.entries[:5]] == [
        "cycling-E1",
        "cycling-E2",
        "cycling-E3",
        "cycling-E4",
        "cycling-E5",
    ]
    assert all(e.name is None for e in library.entries[5:29])
    assert [(e.id, e.name) for e in library.entries[29:]] == [
        ("gym-section-01", "TREN INFERIOR"),
        ("gym-section-02", "TREN SUPERIOR"),
        ("gym-section-03", "CORE"),
    ]

    e1 = library.by_id("cycling-E1")
    assert e1 is not None
    assert e1.source == "cycling" and e1.name == "E1"
    e1_session = e1.content
    assert isinstance(e1_session, Session)
    assert e1_session.blocks[0].role.value == "Warm up"  # typed Session

    first_unlabelled = library.by_id("cycling-session-01")
    assert first_unlabelled is not None
    assert first_unlabelled.name is None
    unlabelled_session = first_unlabelled.content
    assert isinstance(unlabelled_session, Session)
    assert unlabelled_session.label is None

    inferior = library.by_id("gym-section-01")
    assert inferior is not None
    assert inferior.source == "gym" and inferior.name == "TREN INFERIOR"
    assert inferior.content.line == 3  # typed GymSection with provenance

    assert library.by_id("cycling-E99") is None

    assert [e.id for e in library.by_name("E3")] == ["cycling-E3"]
    assert [e.id for e in library.by_name("CORE")] == ["gym-section-03"]
    assert library.by_name("no-such-workout") == ()
