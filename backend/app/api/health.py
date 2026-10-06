"""Health check endpoint."""

from fastapi import APIRouter

from app.core.settings import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Return a minimal health payload for uptime checks."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "engine_version": settings.engine_version,
        "environment": settings.environment,
    }
