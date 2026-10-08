"""RED unit tests for the multipart activity-file upload (ODD tasks FI-1/FI-2).

Endpoint contract (verified from the official Intervals.icu API cookbook,
``POST /athlete/{id}/activities``):

- accepts ``multipart/form-data`` with the file in the form field ``file``,
  plus optional ``name`` and ``description`` fields;
- accepts ``.fit``, ``.gpx``, ``.fit.gz``, ``.gpx.gz`` or a zip containing them;
- returns **201 when at least one activity was created and 200 when everything
  was a duplicate** (dedup by a hash of the file contents — re-uploads are
  safe and must be reported as a duplicate, not an error);
- the response body is a JSON array of the created activities.

Auth follows the client-wide rule: HTTP Basic in the ``Authorization``
header only — the API key must never appear in the request body or query.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.ingest.client import IntervalsClient
from app.ingest.exceptions import (
    IntervalsClientError,
    IntervalsServerError,
)
from app.ingest.models import ActivityUploadResult

BASE_URL = "https://intervals.icu/api/v1"
API_KEY = "secret-key"
FIT_BYTES = b".FIT-FILE-CONTENT-0123456789"


def make_settings(max_retries: int = 3, backoff_factor: float = 0.5) -> Settings:
    """Build explicit settings so tests never depend on ambient env vars."""
    return Settings(
        intervals_api_key=API_KEY,
        intervals_base_url=BASE_URL,
        intervals_athlete_id="0",
        intervals_max_retries=max_retries,
        intervals_backoff_factor=backoff_factor,
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    settings: Settings,
) -> tuple[IntervalsClient, list[httpx.Request]]:
    """Build a client over a recording MockTransport."""
    sent: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    client = IntervalsClient(
        settings=settings, transport=httpx.MockTransport(recording_handler)
    )
    return client, sent


def json_response(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def relative_path(request: httpx.Request) -> str:
    """Path of a request relative to the configured base URL path (/api/v1)."""
    base = httpx.URL(BASE_URL).path
    assert request.url.path.startswith(base)
    return request.url.path[len(base) :]


def parse_multipart(request: httpx.Request) -> dict[str, dict[str, bytes]]:
    """Parse a multipart/form-data request body into ``{name: part}``.

    Each part dict carries ``filename`` (empty for plain form fields),
    ``content_type`` and ``content``.
    """
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data"), content_type
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts: dict[str, dict[str, bytes]] = {}
    for section in request.content.split(b"--" + boundary):
        section = section.strip(b"\r\n")
        if not section or section == b"--":
            continue
        headers_blob, _, body = section.partition(b"\r\n\r\n")
        headers = dict(
            line.split(b": ", 1) for line in headers_blob.split(b"\r\n") if b": " in line
        )
        disposition = headers[b"Content-Disposition"].decode("utf-8")
        name = re.search(r'name="([^"]*)"', disposition)
        assert name is not None, disposition
        filename = re.search(r'filename="([^"]*)"', disposition)
        parts[name.group(1)] = {
            "filename": (filename.group(1) if filename else "").encode(),
            "content_type": headers.get(b"Content-Type", b""),
            "content": body,
        }
    return parts


# --- Request shape --------------------------------------------------------------


def test_upload_posts_multipart_file_field_to_athlete_activities() -> None:
    client, sent = make_client(lambda _: json_response([], status_code=201), make_settings())

    client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert relative_path(request) == "/athlete/0/activities"
    assert request.headers["content-type"].startswith("multipart/form-data")
    parts = parse_multipart(request)
    assert "file" in parts, "the file must be sent in the form field named 'file'"
    assert parts["file"]["filename"] == b"ride.fit"
    assert parts["file"]["content"] == FIT_BYTES


def test_upload_name_and_description_only_sent_when_provided() -> None:
    client, sent = make_client(lambda _: json_response([], status_code=201), make_settings())

    client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    parts = parse_multipart(sent[0])
    assert "name" not in parts, "name must not be sent when not provided"
    assert "description" not in parts, "description must not be sent when not provided"

    client.upload_activity_file(
        FIT_BYTES, filename="ride.fit", name="Morning ride", description="Test"
    )

    parts = parse_multipart(sent[1])
    assert parts["name"]["content"] == b"Morning ride"
    assert parts["description"]["content"] == b"Test"


def test_upload_api_key_never_in_body_or_query() -> None:
    client, sent = make_client(lambda _: json_response([], status_code=201), make_settings())

    client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    request = sent[0]
    assert API_KEY.encode() not in request.content, "API key must never appear in the body"
    assert API_KEY not in str(request.url), "API key must never appear in the query"
    assert request.headers["Authorization"].startswith("Basic "), (
        "the API key belongs in the Authorization header only"
    )


# --- 201 creation ---------------------------------------------------------------

# The REAL response shape, live-verified 2026-10 by uploading one of the
# owner's files twice against Intervals.icu (ODD fit-intake FI-5
# reconnaissance): BOTH 201 (created) and 200 (duplicate) return a JSON
# OBJECT, not an array:
#
#     {"icu_athlete_id":"i555003","id":"i194265861",
#      "activities":[{"icu_athlete_id":"i555003","id":"i194265861"}]}
#
# A 200 duplicate carries the EXISTING activity's id — it is NOT an empty
# body. A bare JSON array is tolerated as a fallback (the API may serve
# both), but the object shape is what the API actually returns.


def observed_object_body(activity_id: str) -> dict[str, Any]:
    """The live-verified response object (created AND duplicate)."""
    return {
        "icu_athlete_id": "i555003",
        "id": activity_id,
        "activities": [{"icu_athlete_id": "i555003", "id": activity_id}],
    }


def test_upload_201_object_body_reports_created_with_activity_ids() -> None:
    client, _ = make_client(
        lambda _: json_response(observed_object_body("i194265861"), status_code=201),
        make_settings(),
    )

    result = client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert isinstance(result, ActivityUploadResult)
    assert result.created is True
    assert result.activity_ids == ("i194265861",)


def test_upload_201_activities_array_ids_are_all_returned() -> None:
    body = {
        "icu_athlete_id": "i555003",
        "id": "i194265861",
        "activities": [
            {"icu_athlete_id": "i555003", "id": "i194265861"},
            {"icu_athlete_id": "i555003", "id": "i194265862"},
        ],
    }
    client, _ = make_client(lambda _: json_response(body, status_code=201), make_settings())

    result = client.upload_activity_file(FIT_BYTES, filename="batch.zip")

    assert result.created is True
    assert result.activity_ids == ("i194265861", "i194265862")


def test_upload_tolerates_bare_array_of_objects() -> None:
    payload = [
        {"id": "i55751783", "icu_athlete_id": "i555003"},
        {"id": "i55751784", "icu_athlete_id": "i555003"},
    ]
    client, _ = make_client(lambda _: json_response(payload, status_code=201), make_settings())

    result = client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert result.created is True
    assert result.activity_ids == ("i55751783", "i55751784")


def test_upload_top_level_id_is_a_fallback_when_activities_is_absent() -> None:
    body = {"icu_athlete_id": "i555003", "id": "i194265861"}
    client, _ = make_client(lambda _: json_response(body, status_code=201), make_settings())

    result = client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert result.created is True
    assert result.activity_ids == ("i194265861",)


def test_upload_result_is_frozen() -> None:
    client, _ = make_client(lambda _: json_response([], status_code=201), make_settings())

    result = client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    with pytest.raises(ValidationError):
        result.created = False


# --- 200 duplicate (carries the EXISTING activity's id — never empty) ------------


def test_upload_200_is_reported_duplicate_with_the_existing_ids() -> None:
    # Live-verified: an identical re-upload returns 200 with the SAME body
    # shape, carrying the EXISTING activity's id.
    client, sent = make_client(
        lambda _: json_response(observed_object_body("i194265861")),
        make_settings(max_retries=3),
    )

    result = client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert len(sent) == 1
    assert result.created is False, "a 200 means everything was a duplicate"
    assert result.activity_ids == ("i194265861",), (
        "the duplicate response names the activity it deduped against"
    )
    # Explicit: not an error and not an empty success.
    assert isinstance(result, ActivityUploadResult)


# --- Error mapping (consistent with the rest of the client) ----------------------


def test_upload_4xx_raises_client_error_without_retry() -> None:
    client, sent = make_client(
        lambda _: json_response({"error": "bad request"}, status_code=400),
        make_settings(max_retries=3),
    )

    with pytest.raises(IntervalsClientError):
        client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert len(sent) == 1, "4xx errors are not retried"


def test_upload_5xx_is_retried_then_raises_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingest import client as client_module

    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)
    client, sent = make_client(
        lambda _: json_response({"error": "boom"}, status_code=500),
        make_settings(max_retries=2),
    )

    with pytest.raises(IntervalsServerError):
        client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert len(sent) == 3, "initial attempt + 2 retries"
    assert len(sleeps) == 2


def test_upload_5xx_retry_can_still_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingest import client as client_module

    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", sleeps.append)
    responses = iter(
        [
            json_response({"error": "boom"}, status_code=500),
            json_response([{"id": "i55751783"}], status_code=201),
        ]
    )
    client, sent = make_client(lambda _: next(responses), make_settings())

    result = client.upload_activity_file(FIT_BYTES, filename="ride.fit")

    assert len(sent) == 2
    assert result.created is True
    assert result.activity_ids == ("i55751783",)
