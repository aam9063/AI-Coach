"""Pydantic models for Intervals.icu activity, stream and wellness payloads.

Payload shapes follow the official Intervals.icu API cookbook; unknown
fields are preserved (``extra="allow"``) so downstream features can read
additional values without a client change. Intervals-side load metrics, if
present, must be treated as non-authoritative cross-check values only (§5.1).

``SportSettings`` and ``AthleteProfile`` model the athlete-threshold endpoints
(``GET /athlete/{id}/sport-settings`` and ``GET /athlete/{id}``); their shapes
were verified against the live Intervals.icu API on 2026-10-02 (owner account:
4 sport-settings entries — Ride/Run/Swim/Other — and one athlete profile with
``icu_resting_hr`` set and ``weight`` null). Threshold values fetched here are
owner-editable configuration (Intervals.icu as the editing surface), never
authoritative computed metrics (§5.1).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Activity(BaseModel):
    """Summary of one activity from ``GET /athlete/{id}/activities``.

    Real Intervals.icu activity ids are strings with an ``i`` prefix (live
    API-verified, e.g. ``i163428838``; the API cookbook itself uses
    ``GET /activity/i55751783/file``). An integer id in a payload is
    defensively coerced to ``str``.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    type: str = ""
    start_date_local: str = ""
    distance: float | None = None
    moving_time: int | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id_to_str(cls, value: object) -> object:
        """Accept a numeric id defensively by coercing it to ``str``."""
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


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


class SportSettings(BaseModel):
    """Per-sport-group threshold configuration from ``GET /athlete/{id}/sport-settings``.

    Shape live-verified on 2026-10-02: one entry per sport group, e.g.
    ``{"id": 2303813, "types": ["Ride", "VirtualRide", ...], "ftp": 180,
    "power_zones": [55, ...], "lthr": 169, "max_hr": 186,
    "hr_zones": [...], "threshold_pace": null, "pace_zones": null}``.
    ``threshold_pace`` is a speed in **m/s** (the swim entry carried
    ``0.8333333``, i.e. CSS speed); cycling entries use ``ftp`` (watts).
    All threshold values here are owner-editable configuration surfaced by
    Intervals.icu, never authoritative computed metrics (§5.1).
    """

    model_config = ConfigDict(extra="allow")

    id: int
    types: list[str] = Field(default_factory=list)
    ftp: float | None = None  # watts (cycling groups; live-verified: 180)
    lthr: float | None = None  # LTHR in bpm (live-verified: 169)
    max_hr: float | None = None  # maximum HR in bpm (live-verified: 186)
    # Speed in m/s (live-verified: 0.8333333 on the swim entry = CSS speed;
    # null on the owner's Ride and Run entries).
    threshold_pace: float | None = None
    power_zones: list[float] | None = None
    hr_zones: list[float] | None = None


class AthleteProfile(BaseModel):
    """Athlete-level values from ``GET /athlete/{id}``.

    Shape live-verified on 2026-10-02: ``id`` is a string with an ``i`` prefix
    (e.g. ``"i555003"`` — defensively coerced if numeric), plus ``name``,
    ``sex``, ``icu_resting_hr`` (65 for the owner) and ``weight`` (null for
    the owner: a real, explicitly reported gap). ``icu_resting_hr`` and
    ``weight`` are owner-editable configuration, never authoritative
    computed metrics (§5.1).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    sex: str | None = None
    icu_resting_hr: float | None = None
    weight: float | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id_to_str(cls, value: object) -> object:
        """Accept a numeric id defensively by coercing it to ``str``."""
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


class ActivityUploadResult(BaseModel):
    """Outcome of ``POST /athlete/{id}/activities`` (multipart file upload).

    The endpoint returns **201 when at least one activity was created and 200
    when everything was a duplicate** (dedup by a hash of the file contents,
    official cookbook), so a 200 is a legitimate, expected outcome — never an
    error. ``created`` distinguishes the two: ``True`` (201) means at least
    one activity was created and ``activity_ids`` carries the ids from the
    response's JSON array of created activities; ``False`` (200) means the
    file was already present — re-uploads are safe no-ops and must be
    reported as a duplicate, not silently dropped (§ fit-intake constraints).
    """

    model_config = ConfigDict(frozen=True)

    created: bool
    activity_ids: tuple[str, ...] = ()
