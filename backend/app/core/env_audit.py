"""Warn about ``.env`` keys the application does not know -- loudly, never fatally.

``Settings`` now tolerates extra entries (``extra="ignore"``), a deliberate
trade-off made when ``LANGFUSE_BASE_URL`` -- a legitimate variable of the
Langfuse SDK, not one of ours -- bricked every process under the previous
``extra="forbid"``. The cost of that tolerance is silence: a typo such as
``INTERVALS_API_KEYY`` does nothing at all and surfaces much later as a
confusing runtime failure, which is exactly what happened on 2026-10-14.

This module restores the loudness without the brittleness. It reads the env
files the process would read, compares their keys against the fields
:class:`~app.core.settings.Settings` actually declares, and logs ONE warning
naming the unknown keys and the file they came from. It never raises, never
changes the tolerance, and is a no-op when a file is absent -- the normal case
inside the containers, where the environment arrives from Compose rather than
from a file (and where a developer's ``.env`` is not mounted at all).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from app.core.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATHS: tuple[Path, ...] = (Path(".env"), Path("backend/.env"))
"""The files a developer's process would read, relative to the CWD.

Both are gitignored and neither exists inside the containers, where the
environment comes from Compose; a missing file is therefore not an error.
"""


def _keys(path: Path) -> list[str]:
    """Key names of a dotenv-style file (blanks and comments ignored)."""
    keys: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return keys


def unknown_env_keys(paths: Sequence[Path] | None = None) -> dict[str, list[str]]:
    """Unknown keys per file, as ``{path: [key, ...]}`` (empty when none).

    A file that cannot be read (missing, unreadable, not UTF-8) is skipped
    rather than reported: this is diagnostic output, never a gate.
    """
    # Settings declares its fields in lower_snake_case while a .env file spells
    # them in UPPER_SNAKE_CASE (pydantic-settings matches either -- its default
    # is case-insensitive), so BOTH sides are normalised here. Comparing raw
    # strings reported every legitimate key as unknown, which this unit's own
    # test caught before the audit could ever fire on a real file.
    known = {field.upper() for field in Settings.model_fields}
    found: dict[str, list[str]] = {}
    for path in DEFAULT_ENV_PATHS if paths is None else paths:
        try:
            keys = _keys(path)
        except (OSError, UnicodeDecodeError):
            continue
        unknown = sorted({key for key in keys if key and key.upper() not in known})
        if unknown:
            found[str(path)] = unknown
    return found


def warn_unknown_env_keys(paths: Sequence[Path] | None = None) -> dict[str, list[str]]:
    """Log one warning per file naming its unknown keys; never raise.

    Returns the same mapping the warning is built from, so a caller or a test
    can assert on it without parsing the log.
    """
    try:
        unknown = unknown_env_keys(paths)
    except Exception:
        logger.debug("env key audit skipped", exc_info=True)
        return {}
    for path, keys in unknown.items():
        logger.warning(
            "unknown key(s) in %s: %s -- Settings ignores unknown entries, so a "
            "typo here would do nothing at all",
            path,
            ", ".join(keys),
        )
    return unknown
