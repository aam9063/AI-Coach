# ADR 0001 — Intervals.icu API verification and rate-limit policy

- **Status**: accepted
- **Date**: 2026-10-02
- **Feature**: `intervals-ingestion` (ING-9), PROJECT_BRIEF.md §5.1, §5.2, §12.2

## Context

Garmin's official Health/Activity API is not available to individuals, and the
unofficial libraries (`garth`, `python-garminconnect`) broke when Garmin changed
its authentication flow in March 2026 (§5.1). The project's decision is that the
owner connects Garmin to Intervals.icu (official Garmin sync) and this app reads
everything from Intervals.icu with a personal API key. Strava is excluded from v1
entirely (§5.3).

Because every downstream feature depends on this single data path, the exact
endpoint paths, authentication shape and rate limits had to be verified against
the official documentation **before** writing the client, rather than assumed
from the ODD task sketch.

## Sources verified

- Intervals.icu API Integration Cookbook (official forum guide, thread 80090):
  `https://forum.intervals.icu/t/intervals-icu-api-integration-cookbook/80090`
- API access to Intervals.icu (official forum guide, thread 609):
  `https://forum.intervals.icu/t/api-access-to-intervals-icu/609`
- OpenAPI UI: `https://intervals.icu/api/v1/docs/` (RapiDoc) and
  `https://intervals.icu/api/v1/docs/swagger-ui/index.html`

## Verified facts

### Base URL and authentication

- Base path: `https://intervals.icu/api/v1`. Configured as
  `INTERVALS_BASE_URL`; the code and tests use relative paths so the base is
  replaceable.
- Personal use: HTTP **Basic** auth with the literal username `API_KEY` and the
  personal API key as the password. The key lives only in the environment
  (`INTERVALS_API_KEY`, §14), never in the repository.
- Third-party apps would need OAuth 2.0 scopes; not applicable to a
  single-owner deployment.

### Endpoints in use

| Purpose | Method and path |
| --- | --- |
| List activities in range | `GET /athlete/{id}/activities?oldest=YYYY-MM-DD&newest=YYYY-MM-DD` |
| Per-second streams | `GET /activity/{activity_id}/streams` |
| Wellness in range | `GET /athlete/{id}/wellness?oldest=YYYY-MM-DD&newest=YYYY-MM-DD` |
| Original activity file | `GET /activity/{activity_id}/file` |
| Intervals-generated FIT | `GET /activity/{activity_id}/fit-file` |

Confirmed details that changed the implementation:

1. **`athlete_id` `0` means the owner of the API key** ("the athlete ID that the
   access_token or API key belongs to should be used"). The default is therefore
   `"0"` and still configurable via `INTERVALS_ATHLETE_ID`.
2. **The original-file endpoint returns gzip-compressed bytes** and the original
   may be FIT, GPX or TCX — it is not guaranteed to be FIT. The client therefore
   decompresses by magic bytes and the FIT parser treats non-FIT bytes as a
   parse failure that falls back to the streams endpoint.
3. **`/fit-file` always returns FIT** (Intervals.icu-generated). It is available
   as a secondary path when the original is not FIT, but the original preserves
   the device's raw fidelity, so ING-5 prefers the original and falls back to the
   streams endpoint.
4. Dates are local calendar dates (`YYYY-MM-DD`), not instants.

### Rate limits

- Observed published limits: **30 requests/second for 1s** and **132 requests
  per 10s**; `429` is returned when exceeded.
- Project policy: pace client calls at **at most 10 req/s** via
  `INTERVALS_MIN_REQUEST_INTERVAL_S` (default `0.1`), enforced by the `Pacer` in
  `app/ingest/sync.py`. This stays well inside the documented burst windows and
  protects the upstream service from a misbehaving backfill.
- `429` and `5xx` responses are retried with exponential backoff
  (`INTERVALS_MAX_RETRIES`, `INTERVALS_BACKOFF_FACTOR`); `4xx` (other than 429)
  are not retried and are mapped to distinct exception types in
  `app/ingest/exceptions.py`.

## Consequences

- Ingestion depends on one external service with a personal key; a revoked or
  rotated key surfaces as an authentication error at the first call, and the
  backfill reports per-window failures instead of a silent partial sync.
- The `<=10 req/s` pacing budget makes the per-day windowing of the backfill
  (ING-7) the dominant cost of a 180-day backfill (~360 calls, ~36s of pacing).
  That is accepted for now because it keeps per-day failure granularity and cheap
  idempotent retries; a chunked-window option remains a possible later
  improvement.
- Raw FIT files are archived (§5.2) so the engine can be re-run when formulas
  change without re-fetching history.
- Intervals.icu's own load metrics are stored **only** in the explicitly
  non-authoritative `activity.intervals_icu_load` cross-check column (§5.1); they
  are never engine truth.

## Verification record

- Endpoints, auth shape, gzip behaviour and the `athlete/0` convention verified
  on 2026-10-02 against the official cookbook and API guide above.
- Client behaviour is covered by unit tests with mocked HTTP transports
  (`backend/tests/ingest/test_client.py`); live verification against a real API
  key remains an owner-side runtime step.
