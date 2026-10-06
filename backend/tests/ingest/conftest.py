"""Ingest-specific fixtures.

The shared ``db_engine``/``db_session``/``anyio_backend`` fixtures live in
``tests/conftest.py`` and target the dedicated test database (see
``tests/dbsupport.py``); this module only holds what is specific to ingest
tests.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_fit_storage_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect the default raw FIT storage root (ING-6) to a temp dir.

    Syncs that do not pass explicit settings build the default storage from
    ``get_settings()``; without this fixture those tests would write FIT
    files into ``backend/data/fit``. The settings cache is cleared so the
    env var is picked up, and again on teardown so later tests see the
    original environment.
    """
    from app.core.settings import get_settings

    monkeypatch.setenv("INGEST_STORAGE_ROOT", str(tmp_path / "fit"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
