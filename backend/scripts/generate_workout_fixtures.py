"""Generate the canonical JSON workout fixtures (WLK-4; ODD workout-library).

The ONLY job of this script is I/O orchestration: it reads the two TXT
sources under ``docs/exercises/``, parses them with the PURE parsers in
``app.agent.workout_library``, and writes the canonical JSON fixtures to
``backend/data/exercises/``. All parsing, projection, and canonicalization
logic lives in the parser module — never here.

The fixtures are GENERATED artifacts committed to git on purpose: the
round-trip regression test
(``tests/agent/test_workout_library_fixtures.py``) asserts that the
committed JSON equals a fresh parse, so drift and silent loss become a
test failure rather than a diff nobody reads.

Usage (from ``backend/``)::

    uv run python scripts/generate_workout_fixtures.py [--out DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Allow running as a plain script: make the backend package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.workout_library import (
    CyclingCorpus,
    GymCorpus,
    cycling_corpus_to_dict,
    gym_corpus_to_dict,
    parse_cycling_corpus,
    parse_gym_corpus,
    to_canonical_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CYCLING_SOURCE = (
    REPO_ROOT / "docs" / "exercises" / "EJEMPLO DE ENTRENAMIENTO PARA CICLISMO.txt"
)
GYM_SOURCE = REPO_ROOT / "docs" / "exercises" / "gym.txt"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "backend" / "data" / "exercises"


def generate_fixtures(cycling_text: str, gym_text: str) -> dict[str, str]:
    """Parse both sources and return {filename -> canonical JSON} (pure)."""
    cycling: CyclingCorpus = parse_cycling_corpus(cycling_text)
    gym: GymCorpus = parse_gym_corpus(gym_text)
    return {
        "cycling.json": to_canonical_json(cycling_corpus_to_dict(cycling)),
        "gym.json": to_canonical_json(gym_corpus_to_dict(gym)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="output directory (default: backend/data/exercises)",
    )
    args = parser.parse_args(argv)

    cycling_text = CYCLING_SOURCE.read_text(encoding="utf-8")
    gym_text = GYM_SOURCE.read_text(encoding="utf-8")
    outputs = generate_fixtures(cycling_text, gym_text)

    args.out.mkdir(parents=True, exist_ok=True)
    for filename, content in outputs.items():
        # newline="\n": canonical fixtures keep LF endings on any platform.
        with (args.out / filename).open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    cycling = parse_cycling_corpus(cycling_text)
    gym = parse_gym_corpus(gym_text)
    print(f"wrote {len(outputs)} fixtures to {args.out}")
    print(
        f"cycling: {len(cycling.sessions)} sessions, {len(cycling.issues)} issues"
    )
    print(
        f"gym: {len(gym.sections)} sections, {len(gym.issues)} issues "
        f"(lines {[i.line for i in gym.issues]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
