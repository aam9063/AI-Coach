"""Raw FIT file storage behind a small interface (ING-6, PROJECT_BRIEF §5.2).

The ingest layer archives every downloaded raw FIT file so the engine can be
re-run later when formulas change (§5.2). Ingestion is allowed to do I/O
(§6), so this module performs real filesystem work.

Implementations:

- :class:`LocalVolumeStorage`: files on a local volume (the Compose
  ``fit-data`` named volume in deployment, a local directory in dev).
  Layout choice (documented ING-6 decision): **flat per-activity-id** —
  files live directly under the root as ``<activity_id>.fit``.
  Intervals.icu is the sole ingestion source (§5.1), so the source
  activity id alone is a stable unique key; ``save`` receives only the id
  (no date), which rules out a YYYY/MM/DD layout without extra plumbing.
- :class:`NullStorage`: inert no-op for the optional disabled mode
  (``ingest_storage_enabled=false``) and for tests.

Writes are atomic: bytes land in a unique temp file in the target
directory and are then renamed into place with ``os.replace`` (atomic on
the same filesystem), so readers never observe a partial file.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from app.core.settings import Settings


class RawFileStorage(Protocol):
    """Storage interface for archived raw FIT files (ING-6, §5.2)."""

    def save(self, activity_id: int, fit_bytes: bytes) -> str:
        """Persist the bytes and return the storage-relative path."""
        ...

    def load(self, path: str) -> bytes:
        """Return the bytes stored at the storage-relative ``path``."""
        ...

    def exists(self, path: str) -> bool:
        """Return whether the storage-relative ``path`` holds a file."""
        ...


class LocalVolumeStorage:
    """Store raw FIT files on a local volume, flat per activity id.

    Paths handed to :meth:`load`/:meth:`exists` must be relative paths as
    returned by :meth:`save`; anything that resolves outside the root
    (``..`` traversal, absolute paths) is rejected with :class:`ValueError`.
    """

    def __init__(self, root_dir: str | os.PathLike[str]) -> None:
        self._root = Path(root_dir)

    def save(self, activity_id: int, fit_bytes: bytes) -> str:
        """Write the bytes atomically as ``<activity_id>.fit`` under the root."""
        self._root.mkdir(parents=True, exist_ok=True)
        relative = f"{activity_id}.fit"
        target = self._resolve(relative)
        fd, tmp_name = tempfile.mkstemp(dir=self._root, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(fit_bytes)
            os.replace(tmp_name, target)
        except BaseException:
            # Never leave a half-written temp file behind.
            with suppress(OSError):
                os.unlink(tmp_name)
            raise
        return relative

    def load(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._resolve(path).is_file()

    def _resolve(self, path: str) -> Path:
        """Resolve a storage-relative path, refusing anything outside the root."""
        if not path or path.startswith(("/", "\\")) or (len(path) > 1 and path[1] == ":"):
            raise ValueError(f"storage paths must be relative to the root: {path!r}")
        candidate = (self._root / path).resolve()
        if not candidate.is_relative_to(self._root.resolve()):
            raise ValueError(f"path escapes the storage root: {path!r}")
        return candidate


class NullStorage:
    """No-op storage for the optional disabled mode and for tests."""

    def save(self, activity_id: int, fit_bytes: bytes) -> str:
        return ""

    def load(self, path: str) -> bytes:
        return b""

    def exists(self, path: str) -> bool:
        return False


def storage_from_settings(settings: Settings) -> RawFileStorage:
    """Build the configured default storage for :func:`sync_date_range`."""
    if settings.ingest_storage_enabled:
        return LocalVolumeStorage(settings.ingest_storage_root)
    return NullStorage()


__all__ = ["LocalVolumeStorage", "NullStorage", "RawFileStorage", "storage_from_settings"]
