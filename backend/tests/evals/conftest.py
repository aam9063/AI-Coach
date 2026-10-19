"""Fixtures for the eval suite: the seams pinned to the dedicated test DB.

The runner executes the REAL pipeline (``process_inbound_message``), so it
needs the same seams the agent tests use (``tests/agent`` convention):

- ``eval_db``: a fresh-schema session factory on the dedicated ``*_test``
  database, installed into the tools' session seam (each golden case runs
  against a freshly created schema, like every other DB test);
- ``eval_settings``: settings pinned for evals — fake LLM and fake
  embeddings (offline, section 14), the eval athlete thresholds the
  prediction/zone cases cite as provenance, and the chart media route
  configured so ``plot_metric`` can hand out signed URLs;
- ``eval_session_repository``: the SDK conversation-memory repository on
  the DEDICATED test database (never the dev one).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.session_store import DbSessionRepository
from app.core.settings import Settings
from tests.dbsupport import test_database_url

EVAL_CHART_SECRET = "eval-chart-secret"
EVAL_CHART_BASE = "https://charts.eval.example.com"


def make_eval_settings(**overrides: Any) -> Settings:
    """The eval Settings: fake providers offline, the documented athlete
    inputs, the chart media route configured. Same inputs the golden.yaml
    provenance comments cite for the prediction and zone expectations."""
    values: dict[str, Any] = {
        "llm_provider": "fake",
        "embedding_provider": "fake",
        "engine_version": "eval-engine-1.0",
        "athlete_ftp_w": 250.0,
        "athlete_lthr_bpm": 169.0,
        "athlete_css_speed_mps": 0.8333333,
        "athlete_threshold_run_speed_mps": 3.33,
        "athlete_weight_kg": 70.0,
        "chart_media_secret": EVAL_CHART_SECRET,
        "chart_public_base_url": EVAL_CHART_BASE,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def golden_results() -> list[Any]:
    """Session-wide collector for the per-case golden results (the
    aggregate threshold gate consumes it — see test_golden_set_thresholds)."""
    return []


@pytest.fixture
async def eval_db(db_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[Any]]:
    """Fresh-schema session factory installed into the tools' seam."""
    factory: async_sessionmaker[Any] = async_sessionmaker(
        db_engine, expire_on_commit=False
    )
    tools.set_session_factory_provider(lambda: factory)
    yield factory
    tools.reset_session_factory_provider()


@pytest.fixture
def eval_settings() -> Iterator[Settings]:
    """Pinned eval settings installed into the tools' settings seam."""
    settings = make_eval_settings()
    tools.set_settings_override(settings)
    yield settings
    tools.reset_settings_override()


@pytest.fixture
def eval_session_repository() -> DbSessionRepository:
    """Conversation-memory repository on the dedicated test database."""
    return DbSessionRepository(test_database_url())
