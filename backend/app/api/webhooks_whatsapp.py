"""WhatsApp inbound webhook (Twilio) — the security gate (§9.1).

Contract (ODD task WA-2, PROJECT_BRIEF §9.1):

- **Signature validation on every request.** ``X-Twilio-Signature`` is
  checked with Twilio's documented algorithm: HMAC-SHA1 keyed with the auth
  token over the request URL followed by every POST parameter sorted by key
  and concatenated as ``key + value``, base64-encoded, compared with
  ``hmac.compare_digest`` (constant time). The algorithm is implemented here
  rather than delegated to ``twilio.request_validator.RequestValidator``:
  the spec is fully documented, the WA-1 tests compute the signature
  independently from the spec (so a wrong implementation cannot pass by
  validating against itself), and ``twilio`` 9.11.2 ships no ``py.typed``
  marker — importing its validator would need a new mypy override outside
  this task's edit surfaces. The vendor library is still used for the
  REST reply (WA-3).
- **Validated against the URL Twilio signed.** When
  ``twilio_public_webhook_url`` is configured (the development tunnel), the
  signature is checked against that PUBLIC URL — validating against the
  locally served URL behind a tunnel would silently reject every real
  message (trap documented on the settings field and in both
  ``.env.example`` files). With no public URL configured, validation still
  always runs (there is no switch to skip it), against the locally served
  request URL — correct for direct local access and tests. Behind a
  development tunnel WITHOUT that variable set, every real message is
  therefore rejected with 403 (fail closed, never skipped).
- **Allowlist.** The sender (``From``) must be in the configured
  comma-separated allowlist; both the entries and the sender are compared
  without the ``whatsapp:`` prefix so either side may carry it.
- **Fail closed.** With no auth token (or no public URL) configured the
  webhook cannot authenticate any caller, so it rejects every request (403)
  instead of skipping validation.
- **Fast ack + hand-off.** The validated message is dispatched to the
  Celery task :func:`app.scheduler.whatsapp_tasks.process_whatsapp_message`
  and the request answers 200 immediately — no LLM or DB work in the request
  path (Twilio retries slow responses).
- **Observability.** Every rejection logs a WARNING with the reason;
  secrets (auth token) never appear in log lines.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request, Response

from app.core.settings import Settings, get_settings
from app.scheduler.whatsapp_tasks import process_whatsapp_message

logger = logging.getLogger(__name__)

router = APIRouter()

SIGNATURE_HEADER = "X-Twilio-Signature"
WHATSAPP_PREFIX = "whatsapp:"


def get_webhook_settings() -> Settings:
    """FastAPI dependency seam so tests can inject dedicated settings."""
    return get_settings()


def compute_twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's documented signature: HMAC-SHA1(url + sorted key+value pairs).

    Exported for clarity; the WA-1 tests compute it independently from the
    spec rather than importing this, so the two implementations cross-check.
    """
    string_to_sign = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(
        auth_token.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def signature_is_valid(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    """Constant-time check of ``signature`` against Twilio's algorithm."""
    expected = compute_twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature)


def normalize_whatsapp_number(number: str) -> str:
    """Strip the ``whatsapp:`` prefix (case-insensitive) and whitespace."""
    cleaned = number.strip()
    if cleaned.lower().startswith(WHATSAPP_PREFIX):
        cleaned = cleaned[len(WHATSAPP_PREFIX) :]
    return cleaned.strip()


def sender_is_allowlisted(settings: Settings, sender: str) -> bool:
    """True when ``sender`` (with or without ``whatsapp:``) is allowlisted."""
    allowed = {
        normalize_whatsapp_number(entry)
        for entry in settings.twilio_whatsapp_allowlist.split(",")
        if entry.strip()
    }
    return normalize_whatsapp_number(sender) in allowed


async def _form_params(request: Request) -> dict[str, str]:
    """Parse Twilio's ``application/x-www-form-urlencoded`` body.

    Parsed manually with ``urllib.parse.parse_qsl`` instead of Starlette's
    ``request.form()``: the body is always urlencoded, and this avoids
    pulling in the ``python-multipart`` extra (a dependency change outside
    this task's scope). Repeated keys keep their last value, which is what
    the signature algorithm's parameter set assumes.
    """
    body = (await request.body()).decode("utf-8")
    return dict(parse_qsl(body, keep_blank_values=True))


def _reject(reason: str) -> Response:
    """Log the rejection (observable, never silent) and fail closed."""
    logger.warning("WhatsApp webhook rejected: %s", reason)
    return Response(status_code=403)


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_webhook_settings)],
) -> Response:
    """Inbound Twilio WhatsApp webhook: validate, allowlist, ack, dispatch."""
    params = await _form_params(request)
    sender = params.get("From", "")

    # Fail closed: without the auth token no request can be authenticated,
    # so nothing is accepted (never "skip validation" — there is no switch).
    if not settings.twilio_auth_token:
        return _reject("webhook security is not configured (missing auth token)")

    signature = request.headers.get(SIGNATURE_HEADER, "")
    # Validate against the URL Twilio signed: the PUBLIC URL (the tunnel)
    # when configured, otherwise the locally served one. See the module
    # docstring and the settings field comment for the tunnel trap.
    signed_url = settings.twilio_public_webhook_url or str(request.url)
    if not signature_is_valid(settings.twilio_auth_token, signed_url, params, signature):
        return _reject(f"missing or invalid X-Twilio-Signature (sender={sender or 'unknown'})")

    if not sender_is_allowlisted(settings, sender):
        return _reject(f"sender not in allowlist (sender={sender})")

    # Fast ack: hand the validated message to the worker and answer
    # immediately — no LLM or DB work in the request path (§9.1).
    process_whatsapp_message.delay(params)
    return Response(status_code=200)
