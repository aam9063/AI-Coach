"""Application settings loaded from environment variables.

Secrets only via environment variables; never commit keys (§14).
Every default is either an inert local-dev value or an empty string —
no real secrets live in this file.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration for the tri-coach backend."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- Application -------------------------------------------------------
    app_name: str = "tri-coach"
    environment: str = "dev"
    debug: bool = False
    # §14: units metric, timezone Europe/Madrid (owner choice).
    timezone: str = "Europe/Madrid"

    # --- Infrastructure ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://tri_coach:tri_coach@localhost:5432/tri_coach"
    redis_url: str = "redis://localhost:6379/0"

    # --- Secrets (empty by default; provided via environment only) ---------
    # §5.1: Intervals.icu is the sole ingestion source.
    intervals_api_key: str = ""
    intervals_athlete_id: str = ""

    # WhatsApp via Twilio (§5.1).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    # Comma-separated list of allowed WhatsApp numbers.
    twilio_whatsapp_allowlist: str = ""

    # --- LLM providers (switchable per §6; both optional placeholders) -----
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # --- Observability ------------------------------------------------------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    # --- Logging / versioning -----------------------------------------------
    log_level: str = "INFO"
    # Exposed on every persisted engine output (§6).
    engine_version: str = "0.1.0"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
