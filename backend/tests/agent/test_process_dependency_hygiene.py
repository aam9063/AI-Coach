"""Process-level agent wiring must not leak between turns or between tests.

Measured 2026-10-19: ``whatsapp_tasks`` calls ``handle_inbound_message(message)``
with no injections, so every WhatsApp turn took the default path and built an
engine with its own pool, an HTTP client for the model, a Twilio client and a
``DbSessionRepository`` -- which starts a thread with its own event loop -- and
closed none of them. That is a leak per message, so the dependencies are now
built once per process.

That fix moves the risk rather than removing it: process-level state can leak
between tests, which is the same class of silent contamination as the shared test
database that cost a whole investigation the same day. These tests pin both
halves -- the cache really reuses, and the autouse fixture really clears it.

The two guard tests are named ``a_`` and ``b_`` on purpose: pytest runs them in
definition order, so the leaky one runs FIRST and the clean-state assertion runs
next. Delete the autouse fixture and the second test fails, which is the whole
point of having it.
"""

from __future__ import annotations

from app.agent import pipeline, tools
from app.core.settings import get_settings


def test_a_a_leaky_test_leaves_process_state_behind() -> None:
    """Populate the process-level state on purpose, for the guard below."""
    tools.set_session_factory_provider(lambda: None)  # type: ignore[arg-type,return-value]
    pipeline._process_session_factory(get_settings())
    assert tools._provider is not None
    assert pipeline._PROCESS_DEPS


def test_b_the_next_test_starts_from_a_clean_process() -> None:
    """What the previous test set is gone: the autouse fixture cleared it.

    This is the real guard. If the reset fixture disappears, the state from
    ``test_a`` survives and this test fails -- the contamination cannot hide.
    """
    assert pipeline._PROCESS_DEPS == {}
    assert tools._provider is None
    assert tools._settings_override is None


def test_c_the_session_factory_is_built_once_per_process() -> None:
    """Two turns must reuse one pool, not create one per WhatsApp message."""
    settings = get_settings()
    first = pipeline._process_session_factory(settings)
    second = pipeline._process_session_factory(settings)
    assert first is second


def test_d_reset_process_dependencies_forgets_everything() -> None:
    pipeline._process_session_factory(get_settings())
    assert pipeline._PROCESS_DEPS
    pipeline.reset_process_dependencies()
    assert pipeline._PROCESS_DEPS == {}
