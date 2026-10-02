"""Shared fixtures for scheduler tests."""

import pytest

from app.scheduler.celery_app import celery_app


@pytest.fixture
def eager_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run Celery tasks eagerly: no broker needed, results inline.

    ``task_eager_propagates=False`` (the default) means an unexpected
    exception inside a task body surfaces as a ``FAILURE`` result state
    instead of re-raising, which is exactly what the failure tests assert.
    """
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)
