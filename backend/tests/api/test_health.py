"""Tests for the /health endpoint of the FastAPI application."""

from fastapi.testclient import TestClient

from app.core.settings import get_settings


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def test_health_returns_200() -> None:
    client = _client()

    response = client.get("/health")

    assert response.status_code == 200


def test_health_payload_fields() -> None:
    client = _client()

    response = client.get("/health")

    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["app"], str)
    assert payload["app"]
    # Every persisted engine output carries engine_version (§6); exposed at app level.
    assert isinstance(payload["engine_version"], str)
    assert payload["engine_version"]
    assert payload["environment"] == get_settings().environment
