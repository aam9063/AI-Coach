"""Synchronous HTTP client for the Intervals.icu API v1.

Endpoint verification source: the official Intervals.icu API cookbook
(https://intervals.icu/api-swagger.json, "API cookbook" section) and the
Intervals.icu forum threads https://forum.intervals.icu/t/80090 and
https://forum.intervals.icu/t/609 (original-file download is GZIP-compressed;
``/activity/{id}/fit-file`` returns a generated always-FIT file).

Endpoints used (§5.1):
- ``GET /athlete/{id}/activities?oldest=YYYY-MM-DD&newest=YYYY-MM-DD``
- ``GET /activity/{id}/streams`` (power, HR, speed, cadence, altitude, distance, time)
- ``GET /athlete/{id}/wellness?oldest=YYYY-MM-DD&newest=YYYY-MM-DD``
- ``GET /athlete/{id}/sport-settings`` (per-sport thresholds; shape live-verified 2026-10-02)
- ``GET /athlete/{id}`` (athlete profile: resting HR, weight, sex; shape live-verified 2026-10-02)
- ``GET /activity/{id}/file`` (original upload; response is GZIP-compressed)
- ``GET /activity/{id}/fit-file`` (generated FIT file)
- ``POST /athlete/{id}/activities`` (multipart activity-file upload; 201 =
  created, 200 = all duplicates — dedup by file-content hash, cookbook-verified)

Auth: HTTP Basic with the literal username ``API_KEY`` and the personal API
key as the password (§5.1). Athlete id ``0`` refers to the key's owner; a
configurable athlete id defaults to ``"0"``.

Rate limits (cookbook: 30/s burst, 132 per 10s): the client targets well
below that and retries 429/5xx responses with exponential backoff
(``intervals_backoff_factor * 2**attempt`` seconds between attempts).
"""

from __future__ import annotations

import gzip
import time
from types import TracebackType
from typing import Any

import httpx

from app.core.settings import Settings, get_settings
from app.ingest.exceptions import (
    IntervalsAuthError,
    IntervalsClientError,
    IntervalsConfigError,
    IntervalsHTTPError,
    IntervalsNotFoundError,
    IntervalsRateLimitError,
    IntervalsServerError,
)
from app.ingest.models import (
    Activity,
    ActivityUploadResult,
    AthleteProfile,
    SportSettings,
    Stream,
    Wellness,
)

# Module-level indirection so tests can observe backoff without real sleeps.
_sleep = time.sleep

_GZIP_MAGIC = b"\x1f\x8b"


class IntervalsClient:
    """HTTP client for the Intervals.icu API v1 (sync ``httpx.Client``).

    A custom ``transport`` (e.g. ``httpx.MockTransport``) may be injected for
    tests; otherwise a standard connection pool is used.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if not self._settings.intervals_api_key:
            raise IntervalsConfigError(
                "intervals_api_key is empty; set INTERVALS_API_KEY via environment"
            )
        self._client = httpx.Client(
            base_url=self._settings.intervals_base_url,
            auth=httpx.BasicAuth("API_KEY", self._settings.intervals_api_key),
            transport=transport,
            timeout=30.0,
        )

    # --- Context-manager sugar ------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> IntervalsClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Public API ------------------------------------------------------------

    def list_activities(self, oldest: str, newest: str) -> list[Activity]:
        """List activities in a date range.

        ``GET /athlete/{id}/activities?oldest=YYYY-MM-DD&newest=YYYY-MM-DD``
        (verified: official API cookbook).
        """
        response = self._request(
            "GET",
            f"/athlete/{self._settings.intervals_athlete_id}/activities",
            params={"oldest": oldest, "newest": newest},
        )
        return [Activity.model_validate(item) for item in response.json()]

    def get_streams(self, activity_id: str) -> list[Stream]:
        """Fetch per-second streams (power, HR, speed, cadence, altitude, distance, time).

        ``GET /activity/{id}/streams`` where ``id`` is the string activity id
        (live-verified form, e.g. ``i163428838``).
        """
        response = self._request("GET", f"/activity/{activity_id}/streams")
        return [Stream.model_validate(item) for item in response.json()]

    def get_wellness(self, oldest: str, newest: str) -> list[Wellness]:
        """Fetch daily wellness records (HRV, resting HR, sleep, weight).

        ``GET /athlete/{id}/wellness?oldest=YYYY-MM-DD&newest=YYYY-MM-DD``
        (verified: official API cookbook).
        """
        response = self._request(
            "GET",
            f"/athlete/{self._settings.intervals_athlete_id}/wellness",
            params={"oldest": oldest, "newest": newest},
        )
        return [Wellness.model_validate(item) for item in response.json()]

    def get_sport_settings(self) -> list[SportSettings]:
        """Fetch the per-sport-group threshold configuration entries.

        ``GET /athlete/{id}/sport-settings`` — response shape verified against
        the live API on 2026-10-02 (owner account: 4 entries — Ride, Run,
        Swim, Other — each with ``ftp``/``lthr``/``max_hr``/``threshold_pace``
        and zone arrays; ``threshold_pace`` is a speed in m/s, e.g. 0.8333333
        on the swim entry, i.e. CSS speed).

        The values returned are owner-editable configuration surfaced by
        Intervals.icu; they are never treated as authoritative computed
        metrics (§5.1).
        """
        response = self._request(
            "GET", f"/athlete/{self._settings.intervals_athlete_id}/sport-settings"
        )
        return [SportSettings.model_validate(item) for item in response.json()]

    def get_athlete_profile(self) -> AthleteProfile:
        """Fetch the athlete profile (resting HR, weight, sex, name).

        ``GET /athlete/{id}`` — response shape verified against the live API
        on 2026-10-02 (string ``id`` with an ``i`` prefix, e.g. ``"i555003"``;
        ``icu_resting_hr`` set, ``weight`` null for the owner). Like
        ``get_sport_settings`` the values are owner-editable configuration,
        never authoritative computed metrics (§5.1).
        """
        response = self._request(
            "GET", f"/athlete/{self._settings.intervals_athlete_id}"
        )
        return AthleteProfile.model_validate(response.json())

    def download_original_file(self, activity_id: str) -> bytes:
        """Download the original uploaded file for an activity as raw bytes.

        ``GET /activity/{id}/file`` — the response body may be
        GZIP-compressed or an uncompressed FIT file (live-verified: a
        request returned an uncompressed FIT, ``b'.FIT'`` at offset 8), so
        gzip is removed here only when the magic bytes say so; other bodies
        pass through unchanged. Decompressed content may be fit/gpx/tcx
        bytes.
        """
        response = self._request("GET", f"/activity/{activity_id}/file")
        return self._gunzip(response.content)

    def download_fit_file(self, activity_id: str) -> bytes:
        """Download the generated always-FIT file for an activity as raw bytes.

        ``GET /activity/{id}/fit-file`` (verified: forum threads 80090/609).
        """
        response = self._request("GET", f"/activity/{activity_id}/fit-file")
        return response.content

    def upload_activity_file(
        self,
        data: bytes,
        *,
        filename: str,
        name: str | None = None,
        description: str | None = None,
    ) -> ActivityUploadResult:
        """Upload an activity file to Intervals.icu as a multipart form.

        ``POST /athlete/{id}/activities`` — accepts ``multipart/form-data``
        with the file in the form field named ``file`` plus optional ``name``
        and ``description`` fields; ``name``/``description`` are only sent
        when provided. The file may be ``.fit``, ``.gpx``, ``.fit.gz``,
        ``.gpx.gz`` or a zip containing them (verified: official API
        cookbook).

        Response semantics (LIVE-VERIFIED 2026-10 by uploading one of the
        owner's real files twice — the cookbook's array/empty-body
        description is wrong): **201 when the activity was created and 200
        when the file was already present** — and in BOTH cases the body is
        a JSON OBJECT::

            {"icu_athlete_id":"i555003","id":"i194265861",
             "activities":[{"icu_athlete_id":"i555003","id":"i194265861"}]}

        A 200 duplicate carries the EXISTING activity's id (it is NOT an
        empty body): the platform de-duplicates by a hash of the file
        contents, so a re-upload of a byte-identical file is safe and is
        reported here as a duplicate WITH the id it matched. Note the dedup
        is byte-identical only: the same ride arriving as a different file
        (e.g. sourced from Garmin Connect vs this upload) creates a SECOND
        activity. The ids carried in ``ActivityUploadResult.activity_ids``
        come from the ``activities`` array, falling back to the top-level
        ``id``; a bare JSON array of objects is tolerated as a documented
        fallback (the API may serve both).

        Auth is unchanged: HTTP Basic in the ``Authorization`` header only —
        the API key never appears in the body or the query (§5.1).
        4xx responses map to the client's typed exceptions and 429/5xx are
        retried with exponential backoff, exactly like every other endpoint.
        """
        form: dict[str, str] = {}
        if name is not None:
            form["name"] = name
        if description is not None:
            form["description"] = description
        response = self._request(
            "POST",
            f"/athlete/{self._settings.intervals_athlete_id}/activities",
            files={"file": (filename, data)},
            data=form,
        )
        return ActivityUploadResult(
            created=response.status_code == 201,
            activity_ids=upload_response_activity_ids(response.json()),
        )

    # --- Internals ---------------------------------------------------------------

    @staticmethod
    def _gunzip(data: bytes) -> bytes:
        """Decompress gzip bodies (detected by magic bytes); pass others through."""
        if data[:2] == _GZIP_MAGIC:
            return gzip.decompress(data)
        return data

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a request, retrying 429/5xx with exponential backoff."""
        max_retries = self._settings.intervals_max_retries
        backoff_factor = self._settings.intervals_backoff_factor
        for attempt in range(max_retries + 1):
            response = self._client.request(
                method, path, params=params, files=files, data=data
            )
            retriable = response.status_code == 429 or response.status_code >= 500
            if not retriable or attempt >= max_retries:
                return self._raise_for_status(response)
            _sleep(backoff_factor * 2**attempt)
        # Unreachable: the loop always returns or raises.
        raise IntervalsServerError(0, "retry loop exited unexpectedly")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> httpx.Response:
        """Map error statuses to distinct exception types; return successes."""
        status = response.status_code
        if status < 400:
            return response
        message = response.text[:500]
        if status in (401, 403):
            raise IntervalsAuthError(status, message)
        if status == 404:
            raise IntervalsNotFoundError(status, message)
        if status == 429:
            raise IntervalsRateLimitError(status, message)
        if status < 500:
            raise IntervalsClientError(status, message)
        raise IntervalsServerError(status, message)


__all__ = [
    "IntervalsClient",
    "IntervalsHTTPError",
    "upload_response_activity_ids",
]


def upload_response_activity_ids(body: Any) -> tuple[str, ...]:
    """Activity ids from an upload response body (live-verified shape).

    The real body is a JSON OBJECT with an ``activities`` array of objects
    carrying ``id`` (and a top-level ``id`` duplicating the single created
    id); a bare JSON array of objects is tolerated as a fallback. A 200
    duplicate body carries the EXISTING activity's id — never discarded.
    Anything unrecognised yields ``()`` (the caller reports it).
    """
    if isinstance(body, dict):
        raw_items: list[Any] = body.get("activities") or []
        top_id = body.get("id")
    elif isinstance(body, list):
        raw_items = list(body)
        top_id = None
    else:
        raw_items, top_id = [], None
    ids = [
        str(item["id"])
        for item in raw_items
        if isinstance(item, dict) and item.get("id") is not None
    ]
    if not ids and top_id is not None:
        ids.append(str(top_id))
    return tuple(ids)
