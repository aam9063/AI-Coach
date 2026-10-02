"""Pydantic models for Intervals.icu activity, stream and wellness payloads.

Payload shapes follow the official Intervals.icu API cookbook; unknown
fields are preserved (``extra="allow"``) so downstream features can read
additional values without a client change. Intervals-side load metrics, if
present, must be treated as non-authoritative cross-check values only (§5.1).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Activity(BaseModel):
    """Summary of one activity from ``GET /athlete/{id}/activities``."""

    model_config = ConfigDict(extra="allow")

    id: int
    name: str = ""
    type: str = ""
    start_date_local: str = ""
    distance: float | None = None
    moving_time: int | None = None


class Stream(BaseModel):
    """One per-second stream from ``GET /activity/{id}/streams``."""

    model_config = ConfigDict(extra="allow")

    type: str
    data: list[float | int] = Field(default_factory=list)


class Wellness(BaseModel):
    """One daily wellness record from ``GET /athlete/{id}/wellness``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str  # date of the record, "YYYY-MM-DD"
    hrv: float | None = None
    resting_hr: float | None = Field(default=None, alias="RHR")
    sleep_minutes: int | None = None
    weight: float | None = None
