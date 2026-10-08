"""WA-1 RED tests: security contract of ``POST /webhooks/whatsapp`` (§9.1).

Every acceptance gate of §9.1 is exercised here against the real app factory:

- missing / invalid ``X-Twilio-Signature`` → 403, message NOT processed;
- valid signature but sender NOT in the allowlist → 403, NOT processed;
- valid signature + allowlisted sender → fast ack (200) and the message is
  handed to the Celery task;
- the endpoint is registered on the real ``create_app()`` factory (a
  regression test fails if the registration is removed);
- the signature is computed INDEPENDENTLY in this file (raw HMAC-SHA1 over
  Twilio's documented string-to-sign) so the implementation is never
  verifying itself;
- signature validation uses the PUBLIC webhook URL (the development tunnel),
  not the local test-server URL — the two differ behind a tunnel, and
  validating against the local URL would silently reject every real message;
- every rejection is observable (a WARNING log with the reason), never silent;
- the webhook fails CLOSED when the auth token is not configured.

No test touches the network: Celery runs eagerly and the task handler is a
patched module-level seam (same pattern as ``tests/scheduler``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.scheduler.celery_app import celery_app

AUTH_TOKEN = "test-auth-token"
# The PUBLIC URL Twilio signs (the development tunnel), which differs from the
# local URL the test client actually posts to (``http://testserver``).
PUBLIC_WEBHOOK_URL = "https://tunnel.example.com/webhooks/whatsapp"
ALLOWED_NUMBER = "+34600000001"
OTHER_NUMBER = "+34600000009"

BASE_PARAMS: dict[str, str] = {
    "MessageSid": "SMtest00000000000000000000000000",
    "From": f"whatsapp:{ALLOWED_NUMBER}",
    "To": "whatsapp:+34600000000",
    "Body": "¿cómo estoy de forma?",
    "ProfileName": "Owner",
}


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute ``X-Twilio-Signature`` independently of the implementation.

    Twilio's documented algorithm ("Validate Twilio requests" security docs):
    HMAC-SHA1 keyed with the auth token over the full request URL followed by
    every POST parameter sorted by key and concatenated as ``key + value``
    (values NOT url-encoded), then base64-encoded. Implemented here from the
    spec — not imported from ``twilio`` or from the app — so a wrong
    implementation cannot pass by validating against itself.
    """
    string_to_sign = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(
        auth_token.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def make_settings(**overrides: Any) -> Settings:
    """Settings for the webhook tests (Twilio fields configured, rest default)."""
    kwargs: dict[str, Any] = {
        "twilio_auth_token": AUTH_TOKEN,
        "twilio_whatsapp_allowlist": ALLOWED_NUMBER,
        "twilio_public_webhook_url": PUBLIC_WEBHOOK_URL,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def build_app(settings: Settings) -> FastAPI:
    """Build the app the tests run against: the real production factory.

    No locally assembled app and no fallback include: the webhook must be
    reachable from ``create_app()`` exactly as deployed (a dedicated
    regression test asserts the registration).
    """
    from app.api.webhooks_whatsapp import get_webhook_settings

    app = create_app()
    app.dependency_overrides[get_webhook_settings] = lambda: settings
    return app


@pytest.fixture
def handler_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str]]]:
    """Eager Celery + patched task seam; records every handed-off message."""
    calls: list[dict[str, str]] = []

    def _record(message: dict[str, str]) -> None:
        calls.append(message)

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)
    monkeypatch.setattr(
        "app.scheduler.whatsapp_tasks.handle_inbound_message", _record
    )
    yield calls


def post_webhook(
    client: TestClient,
    params: dict[str, str],
    *,
    signature: str | None = None,
    sign_url: str = PUBLIC_WEBHOOK_URL,
) -> Any:
    """POST form-encoded params to the webhook like Twilio does."""
    headers = {"X-Twilio-Signature": signature} if signature is not None else {}
    return client.post("/webhooks/whatsapp", data=params, headers=headers)


def client_for(settings: Settings) -> TestClient:
    return TestClient(build_app(settings))


def test_webhook_is_registered_on_the_production_app(
    handler_calls: list[dict[str, str]],
) -> None:
    """Regression guard: POST /webhooks/whatsapp exists on the real factory.

    Fails if the router registration in ``app.main.create_app`` is ever
    removed — the security contract must be reachable as deployed.
    """
    from app.api.webhooks_whatsapp import get_webhook_settings
    from app.main import create_app as build_production_app

    app = build_production_app()
    # Registration check via the public OpenAPI schema (this FastAPI version
    # keeps included routers as lazy ``_IncludedRouter`` entries in
    # ``app.routes``, so raw ``route.path`` matching is not reliable).
    webhook_paths = app.openapi().get("paths", {}).get("/webhooks/whatsapp", {})
    assert "post" in webhook_paths, "webhook router not registered in create_app()"

    app.dependency_overrides[get_webhook_settings] = lambda: make_settings()
    client = TestClient(app)

    response = post_webhook(client, dict(BASE_PARAMS))  # unsigned request

    assert response.status_code == 403
    assert handler_calls == []


def test_missing_signature_is_rejected_and_not_processed(
    handler_calls: list[dict[str, str]],
) -> None:
    client = client_for(make_settings())

    response = post_webhook(client, dict(BASE_PARAMS))

    assert response.status_code == 403
    assert handler_calls == []


def test_invalid_signature_is_rejected_and_not_processed(
    handler_calls: list[dict[str, str]],
) -> None:
    client = client_for(make_settings())

    response = post_webhook(
        client,
        dict(BASE_PARAMS),
        signature=twilio_signature("wrong-token", PUBLIC_WEBHOOK_URL, BASE_PARAMS),
    )

    assert response.status_code == 403
    assert handler_calls == []


def test_non_allowlisted_sender_is_rejected_and_not_processed(
    handler_calls: list[dict[str, str]],
) -> None:
    client = client_for(make_settings())
    params = {**BASE_PARAMS, "From": f"whatsapp:{OTHER_NUMBER}"}

    response = post_webhook(
        client, params, signature=twilio_signature(AUTH_TOKEN, PUBLIC_WEBHOOK_URL, params)
    )

    assert response.status_code == 403
    assert handler_calls == []


def test_valid_allowlisted_request_is_acked_and_dispatched(
    handler_calls: list[dict[str, str]],
) -> None:
    client = client_for(make_settings())

    response = post_webhook(
        client,
        dict(BASE_PARAMS),
        signature=twilio_signature(AUTH_TOKEN, PUBLIC_WEBHOOK_URL, BASE_PARAMS),
    )

    # Fast ack: Twilio retries slow responses, so the webhook answers 200
    # immediately and hands the message to the Celery task (§9.1).
    assert response.status_code == 200
    assert len(handler_calls) == 1
    handed_off = handler_calls[0]
    assert handed_off["Body"] == "¿cómo estoy de forma?"
    assert handed_off["From"] == f"whatsapp:{ALLOWED_NUMBER}"
    assert handed_off["MessageSid"] == BASE_PARAMS["MessageSid"]


def test_signature_is_validated_against_the_public_url_not_the_local_one(
    handler_calls: list[dict[str, str]],
) -> None:
    """The public-URL trap: a signature over the LOCAL URL must be rejected.

    Behind the development tunnel the public URL differs from
    ``http://localhost:8000``; validating against the local URL would
    silently reject every real message.
    """
    client = client_for(make_settings())

    response = post_webhook(
        client,
        dict(BASE_PARAMS),
        signature=twilio_signature(AUTH_TOKEN, "http://testserver/webhooks/whatsapp", BASE_PARAMS),
    )

    assert response.status_code == 403
    assert handler_calls == []


def test_allowlist_is_tolerant_of_the_whatsapp_prefix(
    handler_calls: list[dict[str, str]],
) -> None:
    """Allowlist entries and senders may each carry the ``whatsapp:`` prefix."""
    client = client_for(
        make_settings(twilio_whatsapp_allowlist=f"whatsapp:{ALLOWED_NUMBER}")
    )
    params = {**BASE_PARAMS, "From": ALLOWED_NUMBER}

    response = post_webhook(
        client, params, signature=twilio_signature(AUTH_TOKEN, PUBLIC_WEBHOOK_URL, params)
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1


def test_fails_closed_when_auth_token_is_not_configured(
    handler_calls: list[dict[str, str]],
) -> None:
    """No token configured → reject, never skip validation (fail closed)."""
    client = client_for(
        make_settings(
            twilio_auth_token="",
            twilio_whatsapp_allowlist=ALLOWED_NUMBER,
            twilio_public_webhook_url=PUBLIC_WEBHOOK_URL,
        )
    )

    response = post_webhook(
        client,
        dict(BASE_PARAMS),
        signature=twilio_signature(AUTH_TOKEN, PUBLIC_WEBHOOK_URL, BASE_PARAMS),
    )

    assert response.status_code == 403
    assert handler_calls == []


def test_without_public_url_configured_validation_uses_the_local_url(
    handler_calls: list[dict[str, str]],
) -> None:
    """No public URL configured → validation STILL runs, against the local URL.

    There is no switch to skip validation; the unset public URL only changes
    WHICH URL is checked, so direct local access and the test suite work
    without configuring the tunnel URL.
    """
    client = client_for(make_settings(twilio_public_webhook_url=""))

    response = post_webhook(
        client,
        dict(BASE_PARAMS),
        # Signed over the locally served URL the request actually hits.
        signature=twilio_signature(AUTH_TOKEN, "http://testserver/webhooks/whatsapp", BASE_PARAMS),
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1


def test_rejections_are_observable_in_logs(
    handler_calls: list[dict[str, str]], caplog: pytest.LogCaptureFixture
) -> None:
    """Every rejection is logged (WARNING, with the reason) — never silent."""
    client = client_for(make_settings())
    bad_sender_params = {**BASE_PARAMS, "From": f"whatsapp:{OTHER_NUMBER}"}

    with caplog.at_level(logging.WARNING, logger="app.api.webhooks_whatsapp"):
        post_webhook(client, dict(BASE_PARAMS))  # missing signature
        post_webhook(  # invalid signature
            client,
            dict(BASE_PARAMS),
            signature=twilio_signature("wrong-token", PUBLIC_WEBHOOK_URL, BASE_PARAMS),
        )
        post_webhook(  # valid signature, non-allowlisted sender
            client,
            bad_sender_params,
            signature=twilio_signature(AUTH_TOKEN, PUBLIC_WEBHOOK_URL, bad_sender_params),
        )

    reasons = [record.getMessage() for record in caplog.records]
    assert any("signature" in reason.lower() for reason in reasons)
    assert any("allowlist" in reason.lower() for reason in reasons)
    # The auth token never appears in any log line (secrets stay out of logs).
    assert all(AUTH_TOKEN not in reason for reason in reasons)


def test_missing_signature_handler_call_and_invalid_signature_handler_call(
    handler_calls: list[dict[str, str]],
) -> None:
    """The task seam is only invoked for fully valid requests (belt & braces)."""
    client = client_for(make_settings())

    # Invalid signature over an otherwise valid request must not dispatch.
    tampered = {**BASE_PARAMS, "Body": "tampered"}
    response = post_webhook(
        client,
        tampered,
        signature=twilio_signature(AUTH_TOKEN, PUBLIC_WEBHOOK_URL, BASE_PARAMS),
    )
    assert response.status_code == 403
    assert handler_calls == []
