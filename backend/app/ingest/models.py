"""Pydantic models for Intervals.icu activity, stream and wellness payloads.

Activity/stream shapes follow the official Intervals.icu API cookbook;
unknown fields are preserved (``extra="allow"``) so downstream features
can read additional values without a client change. Intervals-side load
metrics, if present, must be treated as non-authoritative cross-check
values only (§5.1).

The ``Wellness`` shape is LIVE-VERIFIED, not cookbook-derived (2026-10-08,
``GET /athlete/{id}/wellness``, HTTP 200, 22 records; union of wire keys
checked): real key names are ``restingHR``, ``sleepSecs`` (SECONDS),
``sleepScore``, ``hrv`` (rMSSD, ms), ``hrvSDNN`` (SDNN, ms — a different
metric from rMSSD), ``weight``, plus ``sleepQuality``, ``avgSleepingHR``,
``readiness``, ``steps``, ``vo2max``, ``baevskySI``, ``tempRestingHR``,
``soreness``, ``fatigue``, ``mood``, ``motivation``, ``injury`` and
``stress`` (unmapped, preserved via ``extra="allow"``). There is NO
``lnHrv`` key on the wire — ln(hrv) is derived from the genuine rMSSD
value.

``SportSettings`` and ``AthleteProfile`` model the athlete-threshold endpoints
(``GET /athlete/{id}/sport-settings`` and ``GET /athlete/{id}``); their shapes
were verified against the live Intervals.icu API on 2026-10-02 (owner account:
4 sport-settings entries — Ride/Run/Swim/Other — and one athlete profile with
``icu_resting_hr`` set and ``weight`` null). Threshold values fetched here are
owner-editable configuration (Intervals.icu as the editing surface), never
authoritative computed metrics (§5.1).
"""

from __future__ import annotations

import math

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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
    """One daily wellness record from ``GET /athlete/{id}/wellness``.

    Wire names are LIVE-VERIFIED (2026-10-08): ``restingHR`` (bpm),
    ``sleepSecs`` (SECONDS), ``sleepScore``, ``hrv`` (rMSSD, ms),
    ``hrvSDNN`` (SDNN, ms) and ``weight`` (kg). The old cookbook-inferred
    names (``RHR``) are accepted defensively, but the wire name is the
    contract and wins when both are present.

    Units are unambiguous at every boundary: the wire delivers sleep in
    SECONDS (``sleep_seconds``/``sleepSecs``), the stored and consumed
    value is MINUTES (``sleep_minutes``). The seconds→minutes conversion
    happens in exactly ONE place — the model validator below — never at a
    call site.

    ``ln_hrv`` is NOT a wire field (no ``lnHrv`` key exists): it is
    derived here, in exactly one place, as ``ln(rMSSD)`` from the genuine
    ``hrv`` value — never from ``hrvSDNN`` (a different metric), and never
    left ``None`` while a positive ``hrv`` is present.

    Not persisted (no ``wellness_snapshot`` columns exist and migrations
    are immutable): ``hrv_sdnn`` and the unmapped live-verified fields
    (``sleepQuality``, ``avgSleepingHR``, ``readiness``, ``steps``,
    ``vo2max``, ``baevskySI``, ``tempRestingHR``, ``soreness``,
    ``fatigue``, ``mood``, ``motivation``, ``injury``, ``stress``) remain
    accessible via ``extra="allow"`` extras only.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str  # date of the record, "YYYY-MM-DD"
    hrv: float | None = None  # rMSSD in milliseconds (live-verified unit)
    hrv_sdnn: float | None = Field(default=None, alias="hrvSDNN")  # SDNN, ms
    resting_hr: float | None = Field(
        default=None,
        # Wire name "restingHR" (live-verified); "RHR" accepted defensively.
        validation_alias=AliasChoices("restingHR", "RHR"),
    )
    # Wire unit: SECONDS. Alias "sleepSecs" is the live-verified key.
    sleep_seconds: float | None = Field(default=None, alias="sleepSecs")
    # Stored/consumed unit: MINUTES. Derived from ``sleep_seconds`` by the
    # model validator (the single conversion point); may also be set
    # directly via populate_by_name.
    sleep_minutes: int | None = None
    sleep_score: float | None = Field(
        default=None,
        # "sleepScore" is live-verified; "sleepScoreCalculated" kept as a
        # defensive fallback from the earlier mapping.
        validation_alias=AliasChoices("sleepScore", "sleepScoreCalculated"),
    )
    weight: float | None = None

    @model_validator(mode="after")
    def _convert_sleep_seconds_to_minutes(self) -> Wellness:
        """Convert the wire's SECONDS to stored MINUTES (single place).

        Rounded to the nearest minute; a directly supplied
        ``sleep_minutes`` (populate_by_name) is never overwritten.
        """
        if self.sleep_minutes is None and self.sleep_seconds is not None:
            self.sleep_minutes = round(self.sleep_seconds / 60)
        return self

    @property
    def ln_hrv(self) -> float | None:
        """``ln(rMSSD)`` derived from the genuine ``hrv`` value.

        ``lnHrv`` is not on the wire (verified against the live API
        2026-10-08). Derived here — never from ``hrvSDNN`` (SDNN is a
        different metric) — and ``None`` only when ``hrv`` is missing or
        non-positive (the engine requires positive rMSSD values).
        """
        if self.hrv is not None and self.hrv > 0.0:
            return math.log(self.hrv)
        return None


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

    The endpoint returns **201 when the activity was created and 200 when
    the file was already present** (live-verified 2026-10: in BOTH cases
    the body is a JSON object carrying the activity id(s) — a 200 duplicate
    names the EXISTING activity it matched, it is never an empty body).
    ``created`` distinguishes the two: ``True`` (201) means at least one
    activity was created and ``activity_ids`` carries the ids from the
    response's ``activities`` array (top-level ``id`` fallback; a bare
    array tolerated); ``False`` (200) means the file matched an
    already-present activity — re-uploads of byte-identical files are safe
    no-ops and must be reported as a duplicate WITH the matched id, never
    silently dropped (§ fit-intake constraints). Note the dedup is
    byte-identical only: the same ride arriving as a different file (e.g.
    from Garmin Connect vs this upload) creates a SECOND activity.
    """

    model_config = ConfigDict(frozen=True)

    created: bool
    activity_ids: tuple[str, ...] = ()
