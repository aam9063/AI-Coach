"""Application settings loaded from environment variables.

Secrets only via environment variables; never commit keys (§14).
Every default is either an inert local-dev value or an empty string —
no real secrets live in this file.
"""

from functools import lru_cache

from pydantic import Field
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

    # --- Engine constants (LOAD-11; §7, §14) --------------------------------
    # Every constant of the deterministic load engine is explicit, sourced
    # and configurable. The pure engine functions in ``app/engine`` keep the
    # same values as their documented module-constant defaults (the
    # fallback); the read layer (``app.db.daily_load``,
    # ``app.services.daily_load``, ``app.tools.cross_check_pmc``) reads them
    # HERE and passes them in — the engine never imports settings (§6).
    # Performance Manager time constants (Allen & Coggan, "Training and
    # Racing with a Power Meter", Performance Manager Chart).
    # Chronic training load time constant, days.
    engine_tau_ctl_days: float = 42.0
    # Acute training load time constant, days.
    engine_tau_atl_days: float = 7.0
    # Minimum PMC history (days) before the series is flagged confident.
    # OWNER CHOICE: ~2 chronic time constants (2 x 42 d); below it CTL is
    # seed-dominated (§7.2 low-confidence flag).
    engine_min_history_days: int = 90
    # ACWR EWMA time constants (Williams, Trewartha, Cross, Kemp & Stokes
    # 2017): acute 7 days, chronic 28 days.
    engine_acwr_tau_acute_days: float = 7.0
    engine_acwr_tau_chronic_days: float = 28.0
    # Normalized Power rolling window, samples (Coggan NP definition: 30 s
    # on the 1 Hz per-second power stream).
    engine_np_window_samples: int = 30
    # Minimum fraction of valid samples a rolling NP window needs to
    # qualify. OWNER CHOICE: 1.0 (strict — only fully complete windows
    # count); relax explicitly for gap-heavy streams.
    engine_min_valid_fraction: float = 1.0
    # Banister TRIMP exponent coefficients a * e^(b * dHRr) (Banister 1991,
    # "Modeling elite athletic performance"): male 0.64/1.92,
    # female 0.86/1.67.
    engine_trimp_male_a: float = 0.64
    engine_trimp_male_b: float = 1.92
    engine_trimp_female_a: float = 0.86
    engine_trimp_female_b: float = 1.67
    # Which sexed coefficient set the read layer selects; OWNER CHOICE
    # ("male" is today's effective default).
    engine_trimp_sex: str = "male"
    # hrTSS reference duration in minutes (Coggan hrTSS convention: one
    # hour at LTHR = 100 hrTSS).
    engine_trimp_reference_minutes: float = 60.0
    # Strength sRPE TSS-equivalent factor (Foster et al. 2001 session-RPE).
    # OWNER CHOICE (2026-10-05): equivalent-effort anchor — NOT a literature
    # constant and NOT an hrTSS calibration. One hour at RPE 7 (Foster AU =
    # 7 x 60 = 420) is treated as equivalent to one hour at threshold
    # (100 TSS), so the factor is
    #   100 / 420 = 0.2380952380952381  (≈ 0.2381).
    # Consequence: the owner's real 40.3-minute RPE-7 gym session (282.1 AU)
    # becomes ≈ 67 TSS-equivalent — comparable to a one-hour ride (~60 TSS) —
    # where the earlier raw-AU value 1.0 made it 282 and dwarfed every ride.
    # The hrTSS-implied anchor (≈ 0.024, measured by the read-only tool
    # app.tools.calibrate_srpe) was explicitly rejected: it reproduces heart
    # rate's systematic undervaluation of strength work.
    engine_srpe_tss_equivalent_factor: float = Field(
        default=100 / 420,  # 100 TSS per 420 AU (1 h at RPE 7) = 0.2380952380952381
        description=(
            "OWNER CHOICE (2026-10-05): equivalent-effort anchor — one hour "
            "at RPE 7 (420 Foster AU) counts as one hour at threshold "
            "(100 TSS), so the factor is 100/420 = 0.2380952380952381. A "
            "documented owner decision, not a literature constant. It makes "
            "a 40.3-minute RPE-7 gym session (~282 AU) ≈ 67 TSS-equivalent, "
            "comparable to a one-hour ride. The hrTSS-implied factor (~0.024, "
            "see app.tools.calibrate_srpe) was considered and rejected: it "
            "reproduces heart rate's undervaluation of strength work."
        ),
    )
    # Banister impulse-response time constants (Banister 1991; Morton,
    # Fitz-Clarke & Banister 1990): fitness tau1, fatigue tau2, days.
    engine_banister_tau1_days: float = 42.0
    engine_banister_tau2_days: float = 7.0
    # Minimum performance markers before a Banister fit is attempted.
    # OWNER CHOICE: 10 = 2 x 5 free parameters (p0, k1, k2, tau1, tau2),
    # guaranteeing >= 5 residual degrees of freedom in the richest fit.
    engine_banister_min_markers: int = 10
    # Intervals.icu PMC cross-check tolerances (§12.3; cross-check only,
    # §5.1). Relative ±10% OR absolute ±0.5 CTL/ATL points: OWNER-AGREED
    # hybrid rule (relative alone is unusable for a decaying series near
    # zero). Both bounds are inclusive.
    engine_cross_check_relative_tolerance: float = 0.10
    engine_cross_check_absolute_tolerance: float = 0.5
    # Their-side revision-detection threshold in CTL/ATL points. OWNER
    # CHOICE: above the float noise of exact-decay days, below the
    # measured real-data Intervals revisions (0.4462 ATL, 0.4982 CTL).
    engine_cross_check_revision_threshold: float = 0.25

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
