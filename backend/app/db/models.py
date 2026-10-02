"""SQLAlchemy 2.0-style DB models for the ingest schema (ING-3, brief §6).

Tables (brief §6 data model minimum, ingest subset):
- ``activity``: one row per ingested activity. The ``(source, source_id)``
  unique key is the idempotency anchor for Intervals.icu ingestion.
- ``activity_stream``: per-second data arrays (JSONB payload) linked to the
  activity, unique per ``(activity_id, stream_type)``.
- ``wellness``: one daily record per (athlete, date), unique.

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

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
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
    source_id: Mapped[int] = mapped_column(Integer)

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
