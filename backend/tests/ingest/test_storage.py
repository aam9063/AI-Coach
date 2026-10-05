"""ING-6 RED: raw FIT storage tests (PROJECT_BRIEF §5.2).

Covers the small storage interface behind which raw FIT files are archived
so the engine can be re-run later when formulas change:

- ``LocalVolumeStorage``: ``save(activity_id, fit_bytes)`` returns a
  relative path and writes the bytes under a deterministic layout;
  writes are atomic (unique temp file in the same directory + rename, so
  readers never see a partial file and no temp files are left behind);
  ``load(path)`` returns the exact bytes; ``exists(path)`` reflects
  reality; path traversal attempts (``../x``, absolute paths) are rejected.
- ``NullStorage``: inert no-op implementation for the optional disabled
  mode and tests.

Documented ING-6 layout choice: **flat per-activity-id** — files are
stored directly under the storage root as ``<activity_id>.fit``.
Intervals.icu is the sole ingestion source (§5.1), so the source activity
id alone is a stable unique key; ``save`` receives only the id (no date),
which rules out a YYYY/MM/DD layout without extra plumbing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.storage import LocalVolumeStorage, NullStorage

PAYLOAD = b"FIT-bytes-\x00\x01-fixture"


# ---------------------------------------------------------------------------
# LocalVolumeStorage
# ---------------------------------------------------------------------------


def test_save_returns_relative_path_and_writes_bytes(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path)

    relative = storage.save("i163428838", PAYLOAD)

    assert relative == "i163428838.fit"
    assert (tmp_path / relative).read_bytes() == PAYLOAD


def test_save_layout_is_flat_per_activity_id(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path)

    first = storage.save("i163428838", PAYLOAD)
    second = storage.save("i163419945", b"other")

    # Deterministic: same id -> same path; different ids -> different files,
    # all directly under the root (no date or source subdirectories).
    assert first == "i163428838.fit"
    assert second == "i163419945.fit"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "i163419945.fit",
        "i163428838.fit",
    ]


def test_save_overwrites_same_activity_atomically(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path)

    path = storage.save("i163428838", b"old-bytes")
    path_again = storage.save("i163428838", b"new-bytes")

    assert path == path_again == "i163428838.fit"
    # Exactly one file per id: no temp files left behind by either write.
    assert [p.name for p in tmp_path.iterdir()] == ["i163428838.fit"]
    assert (tmp_path / "i163428838.fit").read_bytes() == b"new-bytes"


def test_root_directory_is_created_on_demand(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path / "nested" / "fit")

    relative = storage.save("i163428838", PAYLOAD)

    assert (tmp_path / "nested" / "fit" / relative).read_bytes() == PAYLOAD


def test_load_returns_saved_bytes(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path)
    relative = storage.save("i163419945", PAYLOAD)

    assert storage.load(relative) == PAYLOAD


def test_exists_reflects_saved_files(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path)

    assert storage.exists("i163419945.fit") is False
    storage.save("i163419945", PAYLOAD)
    assert storage.exists("i163419945.fit") is True


@pytest.mark.parametrize("bad_path", ["../escape.fit", "sub/../../escape.fit"])
def test_load_rejects_path_traversal(tmp_path: Path, bad_path: str) -> None:
    storage = LocalVolumeStorage(tmp_path)

    with pytest.raises(ValueError, match="escapes the storage root"):
        storage.load(bad_path)


@pytest.mark.parametrize("bad_path", ["../escape.fit", "/etc/passwd", "C:\\fit\\1.fit"])
def test_exists_rejects_paths_outside_the_root(tmp_path: Path, bad_path: str) -> None:
    storage = LocalVolumeStorage(tmp_path)

    with pytest.raises(ValueError):
        storage.exists(bad_path)


# ---------------------------------------------------------------------------
# NullStorage (disabled mode / tests)
# ---------------------------------------------------------------------------


def test_null_storage_is_inert(tmp_path: Path) -> None:
    storage = NullStorage()

    assert storage.save("i163428838", PAYLOAD) == ""
    assert storage.load("1.fit") == b""
    assert storage.exists("1.fit") is False
    # Nothing was written anywhere under tmp_path.
    assert list(tmp_path.iterdir()) == []
