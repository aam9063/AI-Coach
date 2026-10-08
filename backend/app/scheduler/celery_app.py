"""Minimal Celery application placeholder for the tri-coach scheduler.

The ``worker`` and ``beat`` services in ``infra/docker-compose.yml`` boot from
this module with zero tasks (§12.1): it only proves broker/backend wiring via
``Settings.redis_url``. Real beat jobs (sync, morning brief, post-activity
review, weekly report) arrive with Feature ``scheduled-messages`` (§11).
"""

from celery import Celery

from app.core.settings import get_settings

# Explicit include (ING-8): the Compose worker command loads this module
# directly, so the task module is imported via `include` rather than
# `autodiscover_tasks` — verifiable with `celery -A ... inspect registered`.
# ``whatsapp_tasks`` is the WhatsApp inbound-message hand-off target (§9.1,
# ODD task WA-2): the webhook only validates and dispatches.
TASK_MODULES = ["app.scheduler.tasks", "app.scheduler.whatsapp_tasks"]


def create_celery_app() -> Celery:
    """Create the tri-coach Celery application wired to Redis."""
    settings = get_settings()
    return Celery(
        "tri-coach",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=TASK_MODULES,
    )


celery_app = create_celery_app()
