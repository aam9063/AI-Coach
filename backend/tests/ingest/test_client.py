"""RED unit tests for the Intervals.icu HTTP client (ODD task ING-1).

Covers:
- auth header shape: HTTP Basic with literal username ``API_KEY``;
- date-range query params for activities and wellness;
- 429 rate-limit retry with exponential backoff (stdlib-driven, mocked via
  ``httpx.MockTransport`` — no new test dependencies);
- error mapping: 4xx -> distinct exception types, 5xx retried then raised;
- gzip-decompressed original file download returning raw bytes.

Endpoint facts used here were verified against the official Intervals.icu API
cookbook and forum threads 80090/609 (see app/ingest/client.py docstrings).
"""

from __future__ import annotations

import base64
import gzip
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.settings import Settings
from app.ingest.client import IntervalsClient
from app.ingest.exceptions import (
    IntervalsAuthError,
    IntervalsClientError,
    IntervalsNotFoundError,
    IntervalsRateLimitError,
    IntervalsServerError,
)

BASE_URL = "https://intervals.icu/api/v1"
API_KEY = "secret-key"


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


# --- Auth --------------------------------------------------------------------


def test_auth_header_is_basic_with_api_key_username() -> None:
    client, sent = make_client(lambda _: json_response([]), make_settings())

    client.list_activities("2024-01-01", "2024-01-31")

    assert len(sent) == 1
    header = sent[0].headers["Authorization"]
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    assert decoded == f"API_KEY:{API_KEY}"


# --- Endpoints and date-range params ------------------------------------------


def test_list_activities_sends_date_range_params() -> None:
    client, sent = make_client(lambda _: json_response([]), make_settings())

    client.list_activities("2024-01-01", "2024-01-31")

    request = sent[0]
    assert request.method == "GET"
    assert relative_path(request) == "/athlete/0/activities"
    assert request.url.params["oldest"] == "2024-01-01"
    assert request.url.params["newest"] == "2024-01-31"


def test_list_activities_parses_activity_models() -> None:
    payload = [
        {
            "id": 123,
            "name": "Morning ride",
            "type": "Ride",
            "start_date_local": "2024-01-02T08:00:00+01:00",
            "distance": 42000.0,
        }
    ]
    client, _ = make_client(lambda _: json_response(payload), make_settings())

    activities = client.list_activities("2024-01-01", "2024-01-31")

    assert len(activities) == 1
    assert activities[0].id == 123
    assert activities[0].name == "Morning ride"


def test_get_wellness_sends_date_range_params() -> None:
    client, sent = make_client(lambda _: json_response([]), make_settings())

    client.get_wellness("2024-01-01", "2024-01-31")

    request = sent[0]
    assert relative_path(request) == "/athlete/0/wellness"
    assert request.url.params["oldest"] == "2024-01-01"
    assert request.url.params["newest"] == "2024-01-31"


def test_get_streams_uses_activity_streams_endpoint() -> None:
    payload = [{"type": "power", "data": [0, 100, 250]}]
    client, sent = make_client(lambda _: json_response(payload), make_settings())

    streams = client.get_streams(123)

    assert relative_path(sent[0]) == "/activity/123/streams"
    assert streams[0].type == "power"


# --- 429 retry with exponential backoff ----------------------------------------


def test_429_is_retried_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.ingest.client._sleep", sleeps.append)
    responses = iter(
        [json_response({}, status_code=429), json_response({}, status_code=429), json_response([])]
    )
    client, sent = make_client(lambda _: next(responses), make_settings())

    result = client.list_activities("2024-01-01", "2024-01-31")

    assert result == []
    assert len(sent) == 3, "two 429s then one success"
    assert sleeps == [0.5, 1.0], "backoff = factor * 2**attempt"


def test_429_exhausted_raises_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.ingest.client._sleep", sleeps.append)
    client, sent = make_client(
        lambda _: json_response({}, status_code=429),
        make_settings(max_retries=2),
    )

    with pytest.raises(IntervalsRateLimitError):
        client.get_wellness("2024-01-01", "2024-01-31")

    assert len(sent) == 3, "initial attempt + 2 retries"
    assert len(sleeps) == 2


# --- Error mapping (4xx -> distinct exception types, no retry) -----------------


def test_401_raises_auth_error_without_retry() -> None:
    client, sent = make_client(
        lambda _: json_response({"error": "unauthorized"}, status_code=401),
        make_settings(max_retries=3),
    )

    with pytest.raises(IntervalsAuthError):
        client.list_activities("2024-01-01", "2024-01-31")

    assert len(sent) == 1, "4xx errors are not retried"


def test_404_raises_not_found_error() -> None:
    client, sent = make_client(
        lambda _: json_response({"error": "not found"}, status_code=404),
        make_settings(max_retries=3),
    )

    with pytest.raises(IntervalsNotFoundError):
        client.get_streams(999999)

    assert len(sent) == 1


def test_other_4xx_raises_client_error() -> None:
    client, _ = make_client(
        lambda _: json_response({"error": "bad request"}, status_code=400),
        make_settings(max_retries=3),
    )

    with pytest.raises(IntervalsClientError):
        client.get_wellness("2024-01-01", "2024-01-31")


def test_5xx_is_retried_then_raises_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.ingest.client._sleep", sleeps.append)
    client, sent = make_client(
        lambda _: json_response({"error": "boom"}, status_code=500),
        make_settings(max_retries=2),
    )

    with pytest.raises(IntervalsServerError):
        client.list_activities("2024-01-01", "2024-01-31")

    assert len(sent) == 3, "initial attempt + 2 retries"
    assert len(sleeps) == 2


# --- File downloads ------------------------------------------------------------


def test_download_original_file_gunzips_response() -> None:
    raw_fit = b"FITFILE-CONTENT-1234"
    gzipped = gzip.compress(raw_fit)
    client, sent = make_client(
        lambda _: httpx.Response(200, content=gzipped), make_settings()
    )

    data = client.download_original_file(123)

    assert relative_path(sent[0]) == "/activity/123/file"
    assert data == raw_fit, "gzip-compressed body must be decompressed to raw bytes"


def test_download_fit_file_returns_raw_bytes() -> None:
    client, sent = make_client(
        lambda _: httpx.Response(200, content=b"GENERATED-FIT"), make_settings()
    )

    data = client.download_fit_file(123)

    assert relative_path(sent[0]) == "/activity/123/fit-file"
    assert data == b"GENERATED-FIT"
