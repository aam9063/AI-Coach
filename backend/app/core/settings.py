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

    # --- Engine zone/threshold constants (ZON-11; §7.3, §14) ----------------
    # Every zone/threshold constant of the science engine is explicit,
    # sourced and configurable, mirroring the documented module constants
    # in app/engine/zones.py (which remain the engine's fallbacks; the
    # engine never imports settings, §6). The settings→engine mapping
    # helper app.db.zones_config.zone_constants_from_settings follows the
    # LOAD-11 pattern of app.db.daily_load; nothing in app/ consumes zones
    # yet (Feature 6 will wire the get_zones tool and the proposal flow),
    # so the helper has no caller by design today.
    # Critical Power fit window in seconds (Jones et al. 2019; §7.3): the
    # published 2-20 min window for the linear CP model. LITERATURE.
    engine_cp_fit_window_min_s: float = 120.0
    engine_cp_fit_window_max_s: float = 1200.0
    # Mean-maximal power duration ladder in seconds, comma-separated
    # (1 s, 1, 2, 5, 8, 10, 20, 30, 60 min). OWNER CHOICE: a fixed,
    # chart-friendly ladder; the 20 min point feeds the FTP rule and the
    # 2-20 min points the CP fit.
    engine_mmp_durations_s: str = "1,60,120,300,480,600,1200,1800,3600"
    # Critical Speed fit window in seconds (§7.3: best run efforts between
    # "roughly 3 and 20 minutes"). LITERATURE.
    engine_cs_fit_window_min_s: float = 180.0
    engine_cs_fit_window_max_s: float = 1200.0
    # FTP source precedence, comma-separated permutation of
    # manual,cp_derived,twenty_min_power. OWNER CHOICE: the owner has no
    # power meter; the manually confirmed FTP is the value of record
    # (human-in-the-loop, §7.3).
    engine_ftp_precedence: str = "manual,cp_derived,twenty_min_power"
    # CP-to-FTP factor. STANDARD CONVENTION (the linear-form CP
    # approximates FTP) and documented OWNER CHOICE: 1.0, kept explicit,
    # never buried in the arithmetic.
    engine_cp_to_ftp_factor: float = 1.0
    # FTP = 95% of best 20-minute power. LITERATURE (Allen & Coggan FTP
    # convention; §7.3 fixes the 95%).
    engine_twenty_min_to_ftp_factor: float = 0.95
    # Daniels training-pace intensities, % VO2max. The five published
    # intensity BANDS are LITERATURE (Daniels' Running Formula, 3rd ed.,
    # Human Kinetics 2013: Easy 59-74, Marathon 75-84, Threshold 83-88,
    # Interval 95-100, Repetition 105-120); each default below is the band
    # MIDPOINT — an OWNER-REVIEWABLE choice inside the band.
    engine_daniels_easy_pct_vo2max: float = 66.5
    engine_daniels_marathon_pct_vo2max: float = 79.5
    engine_daniels_threshold_pct_vo2max: float = 85.5
    engine_daniels_interval_pct_vo2max: float = 97.5
    engine_daniels_repetition_pct_vo2max: float = 112.5
    # Swim zone boundaries, % of CSS SPEED, comma-separated (Recovery < 85,
    # Aerobic 85-95, Tempo 95-100, Threshold 100-105, VO2 max >= 105).
    # OWNER-REVIEWABLE CHOICE: §7.3 publishes no swim percentage table
    # (unlike the Coggan power and Friel HR tables); only the 100% anchor
    # is LITERATURE (critical speed IS the threshold intensity,
    # Wakayoshi et al. 1992). The owner should confirm the other three
    # boundaries.
    engine_swim_zone_boundary_pcts: str = "85,95,100,105"
    # Threshold-change detection margin (ZON-9): a proposal requires the
    # new effort to exceed the current model value by strictly more than
    # this RELATIVE fraction. OWNER CHOICE (pending owner confirmation).
    engine_threshold_change_margin: float = 0.05

    # --- Engine readiness/intensity/durability constants (RID-10;
    # §7.4-§7.6, §14) -------------------------------------------------------
    # Every constant of the readiness, intensity-distribution and durability
    # engine is explicit, sourced and configurable, mirroring the documented
    # module constants in app/engine/readiness.py, app/engine/intensity.py
    # and app/engine/durability.py (which remain the engine's fallbacks; the
    # engine never imports settings, §6). The settings→engine mapping helper
    # app.db.engine_readiness_config follows the ZON-11 pattern of
    # app.db.zones_config; nothing in app/ consumes these constants yet
    # (Feature 6 will wire the get_readiness / get_intensity_distribution
    # tools), so the helper has no caller by design today.
    # HRV readiness (§7.4): 7-day rolling ln(rMSSD) mean vs the 60-day own
    # baseline, flagged strictly outside ± 0.5 SD. Window and baseline
    # LITERATURE (Plews et al. 2013); the 0.5 SD smallest-worthwhile-change
    # band LITERATURE (Kiviniemi et al. 2007); the 70% baseline completeness
    # floor (42 of 60 days) OWNER CHOICE — the owner's wellness rows are
    # partly populated and a strict requirement would leave the signal
    # permanently unassessable.
    engine_hrv_window_days: int = 7
    engine_hrv_baseline_days: int = 60
    engine_hrv_band_sd: float = 0.5
    engine_hrv_min_baseline_valid_days: int = 42
    # Resting-HR readiness (§7.4): the as-of value vs the 30-day own
    # baseline. Baseline window LITERATURE (§7.4); the 0.5 SD band (the SWC
    # logic extended to resting HR) and the 70% completeness floor OWNER
    # CHOICE.
    engine_rhr_baseline_days: int = 30
    engine_rhr_band_sd: float = 0.5
    engine_rhr_min_baseline_valid_days: int = 21
    # Sleep readiness (§7.4): duration vs the athlete's own baseline. The
    # brief fixes no sleep window: baseline, band and completeness floor are
    # OWNER CHOICE, kept symmetric with resting HR.
    engine_sleep_baseline_days: int = 30
    engine_sleep_band_sd: float = 0.5
    engine_sleep_min_baseline_valid_days: int = 21
    # Fraction of the HRV rolling window that must carry a measurement
    # before the signal assesses. OWNER CHOICE: strict 1.0 (only fully
    # complete windows count), mirroring the gap rules of app.engine.zones /
    # app.engine.load; relax explicitly for gap-heavy streams.
    engine_min_window_valid_fraction: float = 1.0
    # TSB strictly below this value is the "very negative" adverse signal
    # of the multi-signal warning rule (§7.4). OWNER CHOICE: the brief says
    # "very negative" without a number; -10.0 follows the TrainingPeaks /
    # Friel high-fatigue onset and fires earlier than the deep-overreach
    # zone (-30) on purpose — the rule already requires two or more
    # agreeing signals before suggesting anything.
    engine_tsb_very_negative: float = -10.0
    # 3-zone intensity model (§7.5): first- (LT1) and second- (LT2)
    # threshold cut points per modality as "modality:pct" comma pairs
    # (bike_power % FTP, run_hr / bike_hr % LTHR, swim_pace % CSS). Second
    # thresholds sit at the top of the published Threshold zone (Coggan Z4
    # 106% FTP; Friel Z4 100% LTHR; the Tempo/CSS boundary 100% CSS):
    # LITERATURE (Seiler 2010 for the 3-zone construct). First thresholds
    # sit at the top of each table's last fully aerobic zone: OWNER CHOICE —
    # no source publishes LT1 as an exact percentage.
    engine_first_threshold_pcts: str = (
        "bike_power:76,run_hr:90,bike_hr:90,swim_pace:95"
    )
    engine_second_threshold_pcts: str = (
        "bike_power:106,run_hr:100,bike_hr:100,swim_pace:100"
    )
    # Which source zone table each sport's sessions use. OWNER CHOICE: bike
    # rides use the Coggan power table, runs the Friel run HR table, swims
    # the CSS pace table (the primary intensity modality per §7.3/§7.5).
    engine_sport_modality: str = "run:run_hr,bike:bike_power,swim:swim_pace"
    # Descriptive pattern-comparison reference bands (§7.5; Seiler 2010
    # polarized ~80/20; Stöggl & Sperlich 2014 pyramidal): six percentages
    # Z1_lo,Z1_hi,Z2_lo,Z2_hi,Z3_lo,Z3_hi. The papers report means, not
    # decision bands, so the bands around the published point values are
    # OWNER-REVIEWABLE choices.
    engine_polarized_bands: str = "70,90,0,15,10,30"
    engine_pyramidal_bands: str = "55,75,15,35,5,20"
    # Minimum weekly time in zone (seconds) before a pattern label is
    # reported at all. OWNER CHOICE: 2 h — the brief fixes no number, and at
    # low weekly volume the percentages reflect session choice more than
    # distribution.
    engine_min_pattern_week_seconds: float = 7200.0
    # Time-in-zone pause exclusion (§7.5): a paused sample (speed ~0, HR
    # drifting down) must NOT count as easy-zone (Z1) time — real-data
    # finding: the persisted weekly seconds equalled the sum of the stream
    # spans, not moving_time (week 24: 24439 s vs 20910 s), because stops
    # were classified into the easy zone. A sample whose speed is
    # tolerantly at or below the stopped-speed tolerance is non-moving and
    # contributes nothing; without a speed stream, a time interval at or
    # beyond the gap cap (a multiple of the median positive sample
    # interval) is a pause/recording gap and contributes nothing (the
    # general fallback). Speed tolerance LITERATURE-ADJACENT published
    # default: the Garmin FIT SDK's documented stopped_speed_threshold of
    # 0.1 m/s. Gap-cap multiple OWNER CHOICE (no published convention): 5x
    # the owner's ~3 s median interval (~15 s) admits sampling jitter and
    # excludes real stops. Boundary convention (the project's strict
    # rule): exactly at a limit counts as beyond it. Configurable; see
    # app.engine.intensity.moving_weights.
    engine_pause_speed_tolerance_mps: float = 0.1
    engine_pause_gap_cap_median_multiple: float = 5.0
    # Aerobic decoupling reference band (§7.6): Pa:HR strictly below 5% on
    # long steady sessions suggests good aerobic durability. LITERATURE
    # (Friel; the boundary is strict — exactly 5% counts as beyond).
    engine_decoupling_reference_band: float = 0.05
    # Steadiness guard for decoupling: the halves' intensity measures (NP /
    # NGS) must agree within this relative drift, or the session is rejected
    # as not steady. OWNER CHOICE: no source publishes a steadiness
    # threshold; 15% tolerates normal cardiac drift on a steady long ride
    # while excluding interval-style swings.
    engine_max_half_intensity_drift: float = 0.15
    # Durability trends on long sessions (§7.6; Maunder et al. 2021
    # motivate the trend itself, not these operational numbers — all three
    # are OWNER CHOICE): the "long session" threshold (90 min, below it the
    # decoupling measures pacing rather than durability), the trailing
    # window (84 days = 12 weeks, roughly one training build) and the
    # minimum eligible sessions (two points cannot be distinguished from
    # noise).
    engine_min_long_session_seconds: float = 5400.0
    engine_trend_window_days: int = 84
    engine_min_trend_sessions: int = 3

    # WhatsApp via Twilio (§5.1).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    # Comma-separated list of allowed WhatsApp numbers.
    twilio_whatsapp_allowlist: str = ""
    # Public HTTPS URL of the inbound webhook exactly as Twilio sees it
    # (§9.1), e.g. the development tunnel https://<name>.trycloudflare.com.
    # TRAP (documented for the operator): signature validation MUST run
    # against the URL Twilio signed — behind a tunnel that is THIS URL, and
    # validating against the locally served one (http://localhost:8000)
    # silently rejects every real message with 403.
    # Default (empty): validation still ALWAYS runs (there is no switch to
    # skip it and the webhook fails closed without an auth token), but it
    # validates against the locally served request URL — correct for direct
    # local access and the test suite. Behind a development tunnel you MUST
    # set this variable, or every real message is rejected with 403.
    # FULL URL Twilio signs, INCLUDING the /webhooks/whatsapp path
    # (e.g. https://<tunnel>.trycloudflare.com/webhooks/whatsapp): a bare
    # host is used verbatim and would reject every real message.
    twilio_public_webhook_url: str = ""

    # --- LLM providers (switchable per §6; both optional placeholders) -----
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # --- WhatsApp agent (Feature 6; WA-3/WA-4) -------------------------------
    # Which model object the agent gets — ONE object per §6, chosen purely
    # by configuration. "openai" builds the real strands OpenAIModel from
    # openai_api_key/llm_model_id; "fake" builds the deterministic
    # app.agent.fake_model.FakeModel (tests, dry runs — zero network).
    # Anything else fails loudly instead of silently dialling a provider.
    llm_provider: str = "openai"
    # Model id handed to the provider (strands OpenAIConfig.model_id).
    llm_model_id: str = "gpt-4o-mini"

    # --- WhatsApp agent guardrails (Feature 6; WA-5; §9.2, §14) --------------
    # The tool-calling loop is the Strands Agents SDK's; these are OUR
    # guardrails around it, all documented OWNER CHOICES (§14 style) with no
    # literature or vendor source — sized generously for a single-athlete
    # chat coach while keeping a stuck tool loop from burning the budget.
    # Per-invocation caps (strands Limits), passed on EVERY invocation: max
    # loop turns (one turn = one model call plus the tool executions that
    # follow), max cumulative output tokens and max cumulative
    # input+output tokens of ONE message processing. The SDK checks them at
    # turn boundaries and stops with stop_reason limit_turns /
    # limit_output_tokens / limit_total_tokens; a stop without a final
    # answer gets an explicit guardrail message instead of silence.
    agent_max_turns: int = 8
    agent_max_output_tokens: int = 4000
    agent_max_total_tokens: int = 20000
    # Per-conversation token budget: the sum of totalTokens of every turn in
    # the conversation (the WhatsApp free-form window, the 24 h after the
    # athlete's last message, §9.1), accumulated from
    # result.metrics.accumulated_usage and persisted per turn in
    # message_log. When the accumulated usage reaches this budget the
    # pipeline stops BEFORE invoking the model and replies with an explicit
    # budget message; usage frees up as turns age out of the 24 h window.
    agent_conversation_token_budget: int = 60000

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
