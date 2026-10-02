"""Application settings loaded from environment variables.

Secrets only via environment variables; never commit keys (§14).
Every default is either an inert local-dev value or an empty string —
no real secrets live in this file.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration for the tri-coach backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # A key present but empty (`ATHLETE_WEIGHT_KG=`) means "not configured":
        # it is ignored and the field keeps its default. Without this, copying
        # .env.example to .env as the README instructs raises a validation
        # error for the optional numeric fields (developer-visible regression).
        env_ignore_empty=True,
    )

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
    # Athlete id 0 means the key's owner (verified: official API cookbook).
    intervals_athlete_id: str = "0"
    # Base URL of the Intervals.icu API v1 (verified: official API cookbook).
    intervals_base_url: str = "https://intervals.icu/api/v1"
    # Retries after the initial attempt for 429/5xx (cookbook: 30/s burst,
    # 132/10s; the client targets well below that).
    intervals_max_retries: int = 3
    # Exponential backoff: sleep = factor * 2**attempt seconds.
    intervals_backoff_factor: float = 0.5
    # Minimum interval between client calls during sync orchestration
    # (ING-5 pacing: 0.1s => at most 10 requests/second, well inside the
    # cookbook limits of 30/s burst and 132 per 10s). 0 disables pacing.
    intervals_min_request_interval_s: float = 0.1

    # --- Raw FIT file storage (ING-6, §5.2) --------------------------------
    # Root directory where downloaded raw FIT files are archived so the
    # engine can be re-run when formulas change. Relative to the backend
    # working directory unless an absolute path is given (Compose mounts
    # the fit-data volume at /data/fit and sets INGEST_STORAGE_ROOT).
    ingest_storage_root: str = "data/fit"
    # Disable to skip raw FIT archiving; activities keep raw_file_path NULL.
    ingest_storage_enabled: bool = True

    # --- Athlete thresholds (owner-supplied configuration; §14, §5.1) -------
    # Editable configuration for the deterministic load engine (§7.1).
    # Source: Intervals.icu is the EDITING SURFACE — fetch current values with
    # `python -m app.ingest.thresholds`, which prints ready-to-paste lines.
    # These are NEVER authoritative computed metrics (§5.1): Intervals values
    # are cross-check only, and a missing value stays None (no silent default).
    # Intervals.icu live-verified shapes, 2026-10-02.
    # Functional threshold power in watts (sport-settings "Ride" entry ftp).
    athlete_ftp_w: float | None = None
    # Lactate threshold heart rate in bpm (sport-settings lthr).
    athlete_lthr_bpm: float | None = None
    # Maximum heart rate in bpm (sport-settings max_hr).
    athlete_hr_max_bpm: float | None = None
    # Resting heart rate in bpm (athlete profile icu_resting_hr).
    athlete_hr_rest_bpm: float | None = None
    # Swim critical-swim-speed (CSS) in m/s (sport-settings "Swim" entry
    # threshold_pace; live-verified 0.8333333 m/s on 2026-10-02).
    athlete_css_speed_mps: float | None = None
    # Run threshold speed in m/s (sport-settings "Run" entry threshold_pace;
    # null for this owner — a known, explicitly reported gap).
    athlete_threshold_run_speed_mps: float | None = None
    # Body weight in kg (athlete profile weight; null for this owner).
    athlete_weight_kg: float | None = None

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
