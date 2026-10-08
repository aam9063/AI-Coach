"""SQLAlchemy 2.0-style DB models for the ingest schema (ING-3, brief §6).

Tables (brief §6 data model minimum, ingest subset):
- ``activity``: one row per ingested activity. The ``(source, source_id)``
  unique key is the idempotency anchor for Intervals.icu ingestion.
- ``activity_stream``: per-second data arrays (JSONB payload) linked to the
  activity, unique per ``(activity_id, stream_type)``.
- ``wellness``: one daily record per (athlete, date), unique.
- ``daily_load``: one engine-computed load row per (athlete, date, sport),
  plus a ``combined`` sport row per day (LOAD-10, brief §6); the unique key
  makes the recomputation upserts idempotent.

Owner-entered input (§5.1, LOAD-12): ``activity.rpe`` stores the athlete's
own session RPE as entered in Intervals.icu (``icu_rpe``, scale 1-10). It is
INPUT data the engine consumes (sRPE method for strength sports), unlike the
non-authoritative Intervals cross-check columns, which our engine never
reads as truth.

Non-authoritative cross-checks (§5.1): ``activity.intervals_icu_load`` stores
Intervals.icu's own load metric purely for cross-checking against our engine
(Feature 3). It must never be consumed as the authoritative load value; our
engine computes load from raw streams. The field name names the source
explicitly so no column can be mistaken for an engine-authoritative metric.

``raw_file_path`` stays nullable until ING-6 lands raw FIT storage; the
column exists now so the migration and model are stable.

RID-10 additions (brief §6, engine outputs): ``readiness_snapshot`` (one
structured multi-signal readiness row per athlete+date),
``weekly_intensity`` (one 3-zone distribution row per athlete+ISO
week+sport) and ``session_durability`` (one EF/decoupling row per
activity). All three are engine OUTPUT tables: idempotent upsert targets
keyed by their unique constraints, each row stamped with
``engine_version`` (§6) and ``computed_at``; the status/no-data
decisions are documented on each class.
"""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all tri-coach DB models."""


class ActivityRow(Base):
    """One ingested activity, idempotent per (source, source_id)."""

    __tablename__ = "activity"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_activity_source_source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="intervals")
    # Real Intervals.icu activity ids are strings with an "i" prefix
    # (live-verified, e.g. "i163428838"); the (source, source_id) unique key
    # remains the idempotency anchor.
    source_id: Mapped[str] = mapped_column(String(32))

    type: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255), default="")

    # UTC-aware start time; local start time as provided by the source.
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_time_local: Mapped[str | None] = mapped_column(String(64), default=None)

    # Summary fields the Intervals.icu API provides.
    distance_m: Mapped[float | None] = mapped_column(Float, default=None)
    duration_s: Mapped[int | None] = mapped_column(Integer, default=None)
    elevation_m: Mapped[float | None] = mapped_column(Float, default=None)

    # Raw FIT file storage path; nullable until stored (ING-6).
    raw_file_path: Mapped[str | None] = mapped_column(String(512), default=None)

    # OWNER-ENTERED INPUT DATA (LOAD-12, §5.1): the athlete's own RPE for
    # the session, as entered in Intervals.icu (``icu_rpe``, integer scale
    # 1-10; payload aliases ``session_rpe``/``perceived_exertion``). This
    # is reported INPUT, like duration or distance — NOT a computed metric
    # and NOT one of the non-authoritative Intervals cross-check values
    # (§5.1): the engine CONSUMES it (sRPE method for strength sports).
    # Nullable: NULL means "not entered", never a silent 0. Values outside
    # 1-10 are rejected at ingest with a reportable reason, never stored.
    rpe: Mapped[float | None] = mapped_column(Float, default=None)

    # NON-AUTHORITATIVE (§5.1): Intervals.icu's own load metric, kept only as
    # a cross-check value for tests/verification. Our engine computes the
    # authoritative load from raw streams (Feature 3).
    intervals_icu_load: Mapped[float | None] = mapped_column(Float, default=None)


class ActivityStreamRow(Base):
    """Per-second stream data for one activity and stream type (JSONB array)."""

    __tablename__ = "activity_stream"
    __table_args__ = (
        UniqueConstraint("activity_id", "stream_type", name="uq_activity_stream_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activity.id", ondelete="CASCADE"), index=True
    )
    stream_type: Mapped[str] = mapped_column(String(32))  # power, hr, speed, cadence, ...
    # Per-second values ordered by timestamp; JSONB keeps this queryable and
    # simple while volumes stay sane (brief §6 allows per-second arrays).
    payload: Mapped[list[Any]] = mapped_column(JSONB)


class WellnessRow(Base):
    """One daily wellness record, unique per (athlete, date)."""

    __tablename__ = "wellness"
    __table_args__ = (UniqueConstraint("athlete_id", "date", name="uq_wellness_athlete_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Plain integer for now: athlete_profile arrives with a later feature;
    # no FK yet so the ingest migration stands alone.
    athlete_id: Mapped[int] = mapped_column(Integer, default=1)
    date: Mapped[date] = mapped_column(Date)

    hrv: Mapped[float | None] = mapped_column(Float, default=None)  # rMSSD, ms
    ln_hrv: Mapped[float | None] = mapped_column(Float, default=None)  # ln(rMSSD)
    resting_hr: Mapped[float | None] = mapped_column(Float, default=None)
    sleep_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    sleep_score: Mapped[float | None] = mapped_column(Float, default=None)
    weight: Mapped[float | None] = mapped_column(Float, default=None)  # kg
    # NON-AUTHORITATIVE cross-check columns (§5.1, §12.3; LOAD-10): the PMC
    # values (CTL/ATL) computed by Intervals.icu itself, stored ONLY to
    # cross-check our engine's PMC (``app.engine.pmc``) for the same load
    # inputs. Never consumed as authoritative training state; the column
    # names the source explicitly so neither can be mistaken for truth.
    intervals_icu_ctl: Mapped[float | None] = mapped_column(Float, default=None)
    intervals_icu_atl: Mapped[float | None] = mapped_column(Float, default=None)


class DailyLoadRow(Base):
    """One day of engine-computed training load per sport (LOAD-10, brief §6).

    Row shape decision: **per-sport rows plus one ``combined`` row per day**
    (``sport = "combined"``) — the natural shape given the pure engine's
    :func:`app.engine.pmc.compute_pmc_per_sport`, which produces exactly a
    per-sport series and a combined series. The sport key is the engine's
    normalized (lower-case) activity type, e.g. ``"ride"``/``"run"``/
    ``"swim"``.

    The unique key ``(athlete_id, date, sport)`` is the idempotency anchor:
    recomputing a window upserts the existing rows instead of duplicating
    them.

    Every row carries ``engine_version`` (§6: every persisted engine output
    carries the engine version) and ``computed_at`` (recomputation stamp).
    ``methods`` maps the load method key (``power``/``pace_speed``/``hr``/
    ``srpe``; §7.1 fixed selection order) to the number of activities that
    day+sport used it — the persisted "which method was used" trace;
    ``None`` on days with no contributing activity (rest days).
    """

    __tablename__ = "daily_load"
    __table_args__ = (
        UniqueConstraint("athlete_id", "date", "sport", name="uq_daily_load_athlete_date_sport"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Plain integer matching wellness.athlete_id (no athlete_profile table yet).
    athlete_id: Mapped[int] = mapped_column(Integer, default=1)
    date: Mapped[date] = mapped_column(Date)
    # Normalized engine sport key, or "combined" for the all-sports row.
    sport: Mapped[str] = mapped_column(String(32))

    tss: Mapped[float] = mapped_column(Float)
    ctl: Mapped[float] = mapped_column(Float)
    atl: Mapped[float] = mapped_column(Float)
    tsb: Mapped[float] = mapped_column(Float)

    # Load-method usage trace: {"power"/"pace_speed"/"hr"/"srpe": count}.
    methods: Mapped[dict[str, int] | None] = mapped_column(JSONB, default=None)

    engine_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AthleteProfileRow(Base):
    """The athlete's current thresholds with per-metric provenance (ZON-10).

    Brief §6 ``athlete_profile`` ("thresholds and their history"): the
    CURRENT threshold values live here, one row per athlete (unique
    ``athlete_id``); every change is recorded in the append-only
    :class:`AthleteThresholdHistoryRow`, never by editing history.

    Provenance (§7.3 "FTP source must always be stated", ZON-3/ZON-10):
    every threshold value carries a ``*_source`` column holding the
    machine-readable key of the source that produced it. The keys REUSE the
    engine's own vocabulary: the three :data:`app.engine.zones.FTP_SOURCE_KEYS`
    (``manual`` / ``cp_derived`` / ``twenty_min_power``) for FTP, plus the
    name of the engine function that produced a fitted value
    (``fit_critical_power`` / ``fit_critical_speed`` /
    ``css_from_time_trials``). ``manual`` is valid for every metric (an
    owner-entered value). Nullable per metric: NULL means "not yet
    established", never a silent 0.

    Athlete identity (ZON-10 decision, documented):
    - ``athlete_id`` is the LOCAL single-athlete integer used by the
      existing ``wellness.athlete_id`` and ``daily_load.athlete_id``
      columns (default 1). Those pre-existing integer columns are NOT
      rewritten and get no FK (the owner's Intervals athlete id is the
      string ``i555003``, not an integer); this row is the profile they
      refer to.
    - ``intervals_athlete_id`` maps that local integer to the EXTERNAL
      Intervals.icu identity (the string ``"i555003"`` — real Intervals
      ids carry an ``i`` prefix, live-verified at ingest, ING-2). It is
      the join key for future Intervals-side lookups, unique, nullable
      until set (tests and seeded rows may exist without it).
    """

    __tablename__ = "athlete_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Local single-athlete integer, matching the wellness/daily_load
    # convention (default 1). Unique: exactly one profile row per athlete.
    athlete_id: Mapped[int] = mapped_column(Integer, default=1, unique=True)
    # External Intervals.icu athlete id (string, e.g. "i555003"); see the
    # identity decision in the class docstring.
    intervals_athlete_id: Mapped[str | None] = mapped_column(
        String(32), default=None, unique=True
    )

    # Current thresholds, each with the source that produced it.
    ftp_watts: Mapped[float | None] = mapped_column(Float, default=None)
    ftp_source: Mapped[str | None] = mapped_column(String(32), default=None)
    cp_watts: Mapped[float | None] = mapped_column(Float, default=None)
    cp_source: Mapped[str | None] = mapped_column(String(32), default=None)
    # Auxiliary CP-fit output (no proposal flow of its own; informational).
    w_prime_joules: Mapped[float | None] = mapped_column(Float, default=None)
    cs_mps: Mapped[float | None] = mapped_column(Float, default=None)
    cs_source: Mapped[str | None] = mapped_column(String(32), default=None)
    # Auxiliary CS-fit output.
    d_prime_meters: Mapped[float | None] = mapped_column(Float, default=None)
    css_mps: Mapped[float | None] = mapped_column(Float, default=None)
    css_source: Mapped[str | None] = mapped_column(String(32), default=None)

    # §6: every persisted engine output carries the engine version.
    engine_version: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReadinessSnapshotRow(Base):
    """One structured multi-signal readiness snapshot per (athlete, date)
    (RID-10, brief §6/§7.4).

    Row shape decision: **one row per athlete and day**, keyed by the
    unique constraint ``uq_readiness_snapshot_athlete_date`` — the same
    daily identity as the ``wellness`` input row and the ``daily_load``
    ``combined`` row that feed it. The §7.4 structured multi-signal output
    is persisted in full as JSONB (``signals``: one entry per signal key,
    in the engine's stable :data:`app.engine.readiness.SIGNAL_KEYS` order,
    each with status/direction/confidence/observed/baseline evidence) —
    never collapsed into a composite score (§7.4 forbids one).

    The warning-rule result is stored on the row as the headline columns
    ``agreement_count`` and ``suggest_reduce_intensity`` (queryable)
    alongside ``adverse_signal_keys`` and ``reasons`` (JSONB) and the
    suggestion text. The context inputs (``tsb``,
    ``tsb_very_negative_below``, ``subjective_fatigue_reported``,
    ``acwr`` — context only, §7.2) round-trip for audit.

    ``insufficient_data`` representation: a signal whose evidence was
    insufficient persists inside ``signals`` with
    ``status == "insufficient_data"`` and NULL observed/baseline values —
    the absence is reported, never substituted (§7.4, ODD data note).

    Every row carries ``engine_version`` (§6: every persisted engine
    output carries the engine version) and ``computed_at``.
    """

    __tablename__ = "readiness_snapshot"
    __table_args__ = (
        UniqueConstraint("athlete_id", "date", name="uq_readiness_snapshot_athlete_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, default=1)
    date: Mapped[date] = mapped_column(Date)

    # Structured multi-signal payload (list of per-signal dicts, stable
    # SIGNAL_KEYS order) — the §7.4 object, not a score.
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)

    # Warning-rule result (RID-3): headline columns plus the evidence.
    adverse_signal_keys: Mapped[list[str]] = mapped_column(JSONB)
    agreement_count: Mapped[int] = mapped_column(Integer)
    suggest_reduce_intensity: Mapped[bool] = mapped_column(Boolean)
    suggestion: Mapped[str | None] = mapped_column(Text, default=None)
    reasons: Mapped[list[str]] = mapped_column(JSONB)

    # Context inputs of the assessment (round-tripped for audit; ACWR is
    # context only, §7.2).
    tsb: Mapped[float] = mapped_column(Float)
    tsb_very_negative_below: Mapped[float] = mapped_column(Float)
    subjective_fatigue_reported: Mapped[bool | None] = mapped_column(
        Boolean, default=None
    )
    acwr: Mapped[float | None] = mapped_column(Float, default=None)

    engine_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WeeklyIntensityRow(Base):
    """One 3-zone intensity distribution row per (athlete, ISO week, sport)
    (RID-10, brief §6/§7.5).

    Row shape decision: **one row per athlete, ISO week and sport** — the
    identity of the engine's :class:`app.engine.intensity.WeeklySportZones`
    output (the ``WeeklyIntensity`` wrapper holds one sport entry each, so
    the per-sport entry is the natural persisted unit). The unique
    constraint ``uq_weekly_intensity_athlete_year_week_sport`` anchors the
    idempotent recompute upserts. ``week_start`` (the Monday) is stored
    redundantly next to ``(iso_year, iso_week)`` for queryability; it is
    derived from them (ISO 8601, Monday start).

    ``no_data`` representation (§7.5, ODD decision): a sport-week without
    a session of that sport persists as a row with ``status = "no_data"``,
    zero seconds and ``percentages IS NULL`` — a missing week is reported
    AS missing, never as a fabricated 0% split. The same NULL-percentages
    convention covers a ``"data"`` week whose sessions carried no intensity
    data (zero total seconds; nothing to divide by).

    Every row carries ``engine_version`` (§6) and ``computed_at``.
    """

    __tablename__ = "weekly_intensity"
    __table_args__ = (
        UniqueConstraint(
            "athlete_id",
            "iso_year",
            "iso_week",
            "sport",
            name="uq_weekly_intensity_athlete_year_week_sport",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, default=1)
    iso_year: Mapped[int] = mapped_column(Integer)
    iso_week: Mapped[int] = mapped_column(Integer)
    # The Monday of the ISO week (derived from (iso_year, iso_week)).
    week_start: Mapped[date] = mapped_column(Date)
    # Engine sport key ("run"/"bike"/"swim").
    sport: Mapped[str] = mapped_column(String(32))
    # "data" or "no_data" (engine WeekStatus).
    status: Mapped[str] = mapped_column(String(16))

    z1_seconds: Mapped[float] = mapped_column(Float)
    z2_seconds: Mapped[float] = mapped_column(Float)
    z3_seconds: Mapped[float] = mapped_column(Float)
    total_seconds: Mapped[float] = mapped_column(Float)
    # [z1_pct, z2_pct, z3_pct]; NULL when the total is zero (no_data or a
    # session without intensity data) — never a fabricated split.
    percentages: Mapped[list[float] | None] = mapped_column(JSONB, default=None)

    engine_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionDurabilityRow(Base):
    """One aerobic-durability result per activity (RID-10, brief §6/§7.6).

    Row shape decision: **one row per activity**, keyed by the unique
    constraint ``uq_session_durability_activity`` — §6 places "EF,
    decoupling" among the per-activity engine summary metrics, and the
    durability output (§7.6) is inherently per session. The row references
    ``activity.id`` (the engine consumes that row's streams) with the same
    ``ondelete CASCADE`` convention as ``activity_stream``; a separate
    table rather than new columns on ``activity`` keeps the ingest-owned
    row untouched and carries the §6 ``engine_version``/``computed_at``
    recompute stamp the activity columns would lack.

    ``not_steady`` representation (§7.6, ODD decision): a session rejected
    by the steadiness guard persists as a row with ``status =
    "not_steady"``, NULL EF/decoupling fields and the measured intensity
    drift plus the engine ``detail`` — the rejection is evidence, never a
    fabricated decoupling value.

    The §7.6 durability TREND (RID-9) is deliberately NOT persisted: it is
    a pure function of the per-session ``decoupling`` values persisted
    here (joined to ``activity`` for date/duration eligibility), so a
    stored trend row would be a redundant copy. Recompute from this table
    instead.

    Every row carries ``engine_version`` (§6) and ``computed_at``.
    """

    __tablename__ = "session_durability"
    __table_args__ = (
        UniqueConstraint("activity_id", name="uq_session_durability_activity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activity.id", ondelete="CASCADE"), index=True
    )
    # Engine durability sport key ("bike"/"run").
    sport: Mapped[str] = mapped_column(String(16))
    # "ok" or "not_steady" (engine SessionDecouplingStatus).
    status: Mapped[str] = mapped_column(String(16))

    # NULL when not_steady — the guard rejected the session.
    ef_first_half: Mapped[float | None] = mapped_column(Float, default=None)
    ef_second_half: Mapped[float | None] = mapped_column(Float, default=None)
    decoupling: Mapped[float | None] = mapped_column(Float, default=None)
    decoupling_pct: Mapped[float | None] = mapped_column(Float, default=None)
    within_reference_band: Mapped[bool | None] = mapped_column(
        Boolean, default=None
    )
    reference_band: Mapped[float] = mapped_column(Float)
    intensity_first_half: Mapped[float | None] = mapped_column(
        Float, default=None
    )
    intensity_second_half: Mapped[float | None] = mapped_column(
        Float, default=None
    )
    intensity_drift: Mapped[float] = mapped_column(Float)
    max_intensity_drift: Mapped[float] = mapped_column(Float)
    n_samples: Mapped[int] = mapped_column(Integer)
    n_first_half: Mapped[int] = mapped_column(Integer)
    n_second_half: Mapped[int] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")

    engine_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AthleteThresholdHistoryRow(Base):
    """APPEND-ONLY history of threshold proposals and decisions (ZON-10).

    Brief §6 ("thresholds and their history") / §7.3 ("propose (not apply)
    an update; athlete confirms via WhatsApp; store history"): one row per
    recorded decision, written by the confirmation flow
    (:mod:`app.services.athlete_profile`) when the athlete accepts or
    declines a :class:`app.engine.zones.ThresholdChangeProposal`.

    Rows are never updated or deleted (append-only audit trail):

    - ``decision = "accepted"``: the profile was updated to ``new_value``
      (== ``proposed_value``); ``source`` names the provenance of the
      accepted value.
    - ``decision = "declined"``: the profile was left untouched;
      ``new_value`` is NULL and ``proposed_value`` records what was
      refused.

    Every row carries the prior value, the evidence (the proposal's
    justification text), the confirming actor (``confirmed_by`` — the
    WhatsApp channel lands in Feature 6; until then the thin flow records
    the actor it is told to), the ``engine_version`` (§6) and the
    ``recorded_at`` timestamp.

    ``athlete_id`` is the same local single-athlete integer as on
    :class:`AthleteProfileRow` (no FK — same convention as
    ``wellness``/``daily_load``, whose pre-existing integer columns this
    feature deliberately does not rewrite).
    """

    __tablename__ = "athlete_threshold_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    # One of app.engine.zones.THRESHOLD_METRIC_KEYS (validated by the flow).
    metric: Mapped[str] = mapped_column(String(32))
    # "accepted" or "declined" (validated by the flow).
    decision: Mapped[str] = mapped_column(String(16))
    prior_value: Mapped[float | None] = mapped_column(Float, default=None)
    # The value actually applied (accepted); NULL when declined.
    new_value: Mapped[float | None] = mapped_column(Float, default=None)
    # The proposal's proposed value — what was asked for (always present).
    proposed_value: Mapped[float] = mapped_column(Float)
    # The proposal's evidence text (the §7.3 justification).
    evidence: Mapped[str] = mapped_column(Text, default="")
    # The confirming actor (e.g. "owner_whatsapp" once Feature 6 lands).
    confirmed_by: Mapped[str] = mapped_column(String(64))
    # Provenance of the accepted value (NULL for declined proposals).
    source: Mapped[str | None] = mapped_column(String(32), default=None)
    engine_version: Mapped[str] = mapped_column(String(32))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SubjectiveLogRow(Base):
    """OWNER-ENTERED daily subjective report (WA-6, brief §9.3/§7.4).

    One row per (athlete, date), unique — the idempotency anchor for
    logging the day's report twice from the conversation (an update,
    never a duplicate). Like ``activity.rpe`` (LOAD-12), this is
    OWNER-ENTERED INPUT DATA the engine consumes — not an engine
    output — so it carries no ``engine_version``; ``recorded_at`` is the
    audit stamp of the last write.

    Columns (scales documented here and in the tool's docstring, which
    is what the model reads):

    - ``rpe``: the owner's own reported RPE, the 1-10 scale (same scale
      as ``activity.rpe``/``icu_rpe``). NULL = not reported. Values
      outside 1-10 are rejected by the tool with a reportable reason,
      never stored silently (the LOAD-12 rule).
    - ``fatigue``: reported fatigue, 1-10 (1 = no fatigue at all,
      10 = extreme fatigue). NULL = not reported. The readiness engine
      consumes this as a BOOL context signal ("subjective fatigue
      reported", §7.4): ANY reported level counts; a NULL never does.
    - ``soreness``: reported soreness, 1-10 (1 = none, 10 = extreme).
      NULL = not reported. Recorded for the owner's history; the
      readiness engine consumes only the fatigue signal today.
    - ``notes``: free text from the conversation.
    """

    __tablename__ = "subjective_log"
    __table_args__ = (
        UniqueConstraint("athlete_id", "date", name="uq_subjective_log_athlete_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Same local single-athlete integer as wellness/daily_load (default 1).
    athlete_id: Mapped[int] = mapped_column(Integer, default=1)
    date: Mapped[date] = mapped_column(Date)

    rpe: Mapped[float | None] = mapped_column(Float, default=None)
    fatigue: Mapped[int | None] = mapped_column(Integer, default=None)
    soreness: Mapped[int | None] = mapped_column(Integer, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _utcnow() -> datetime:
    """Timezone-aware UTC now (message_log.created_at default)."""
    return datetime.now(UTC)


class MessageLogRow(Base):
    """WhatsApp conversation log: inbound/outbound messages, tool calls (WA-3).

    Brief §6 ``message_log`` ("inbound/outbound messages, tool calls, trace
    id"): the WhatsApp agent's per-turn audit trail. One table, three
    ``direction`` values:

    - ``inbound``: a validated Twilio message. ``message_sid`` carries the
      Twilio ``MessageSid`` and the UNIQUE constraint
      ``uq_message_log_message_sid`` is the IDEMPOTENCY ANCHOR: a Twilio
      retry of the same message cannot produce a second inbound row (Postgres
      rejects it), so the Celery pipeline can never double-process — and
      therefore never double-reply — one message. The anchor is the database
      itself, not an in-memory check, so it survives worker restarts and
      concurrent retries.
    - ``outbound``: the reply actually handed to the Twilio REST client.
      ``message_sid`` stays NULL — outbound rows are not keyed by Twilio ids
      at insert time, and Postgres unique constraints admit multiple NULLs,
      so many outbound/tool rows may share one conversation.
    - ``tool_call``: one agent tool invocation. ``tool_name`` names the tool
      and ``payload`` (JSONB) records the input and the output the agent saw,
      so every number in a reply is auditable back to its engine source (§3).

    ``trace_id`` groups every row written by one processing turn (a fresh
    UUID per turn; the Langfuse trace id arrives with WA-11 and will reuse
    this column's semantics).

    Provenance: messages and tool calls are NOT engine outputs, so they do
    not carry ``engine_version``; the equivalent provenance column is
    ``agent_version`` (engine_version-style, §6) — the version of the agent
    pipeline (prompt + tool set) that produced the turn, so a reply can be
    attributed to the code that generated it.
    """

    __tablename__ = "message_log"
    __table_args__ = (
        UniqueConstraint("message_sid", name="uq_message_log_message_sid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "inbound" | "outbound" | "tool_call" (validated by the pipeline).
    direction: Mapped[str] = mapped_column(String(16))
    # Twilio MessageSid; the idempotency anchor. NULL for outbound/tool rows.
    message_sid: Mapped[str | None] = mapped_column(String(64), default=None)
    # Message text; NULL for tool_call rows.
    body: Mapped[str | None] = mapped_column(Text, default=None)
    # tool_call rows only.
    tool_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # tool_call rows: {"input": ..., "output": ...}; other rows may carry
    # auxiliary metadata (e.g. the Twilio outbound sid).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # Groups all rows written by one processing turn.
    trace_id: Mapped[str] = mapped_column(String(64))
    # engine_version-style provenance (§6) for the agent pipeline.
    agent_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
