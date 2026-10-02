"""Minimal Celery application placeholder for the tri-coach scheduler.

The ``worker`` and ``beat`` services in ``infra/docker-compose.yml`` boot from
this module with zero tasks (§12.1): it only proves broker/backend wiring via
``Settings.redis_url``. Real beat jobs (sync, morning brief, post-activity
review, weekly report) arrive with Feature ``scheduled-messages`` (§11).
"""

from celery import Celery

from app.core.settings import get_settings


def create_celery_app() -> Celery:
    """Create the tri-coach Celery application wired to Redis."""
    settings = get_settings()
    return Celery(
        "tri-coach",
        broker=settings.redis_url,
        backend=settings.redis_url,
    )


celery_app = create_celery_app()
