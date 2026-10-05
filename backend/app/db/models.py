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
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
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
