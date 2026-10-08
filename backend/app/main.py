"""FastAPI application factory."""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.webhooks_whatsapp import router as webhooks_whatsapp_router


def create_app() -> FastAPI:
    """Create and configure the tri-coach FastAPI application."""
    app = FastAPI(title="tri-coach")
    app.include_router(health_router)
    app.include_router(webhooks_whatsapp_router)
    return app


app = create_app()
