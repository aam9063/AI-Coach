# ODD Feature: intervals-ingestion

Status: pending | Feature 2 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Build the ingestion layer that pulls the athlete's activities, per-second streams, wellness data and original FIT files from the Intervals.icu API into PostgreSQL and raw file storage. This is the single data path into the app (PROJECT_BRIEF.md §5.1) and feeds every downstream feature: the engine, charts, predictions and the agent's tools.

## Problem

Garmin's official Health/Activity API is not available to individuals and the unofficial libraries (`garth`, `python-garminconnect`) broke when Garmin changed its auth flow in March 2026 (PROJECT_BRIEF.md §5.1). The brief's decision is that the owner connects Garmin to Intervals.icu (official Garmin sync) and this app reads everything from Intervals.icu with a personal API key. Without a reliable, idempotent ingestion path, the engine has no data and no history to seed baselines (§7.2 requires at least 90 days).

## Scope

- Intervals.icu HTTP client under `backend/app/ingest/` (§6), using `httpx` (§6), HTTP Basic auth with username `API_KEY` and the personal key (§5.1).
- Endpoints in use (§5.1), verifying exact paths and params against the official Swagger/API cookbook before coding:
  - `GET /athlete/{id}/activities?oldest=YYYY-MM-DD&newest=YYYY-MM-DD`
  - `GET /activities/{id}/streams` (power, HR, speed, cadence, altitude, distance, time)
  - `GET /athlete/{id}/wellness?oldest=...&newest=...` (HRV, resting HR, sleep, weight)
  - Original FIT file download for an activity (exact path from the API cookbook)
- FIT parsing with `fitdecode` (§5.2, §6) for second-by-second streams and swim lengths.
- Raw FIT storage (object storage or local volume) so the engine can be re-run when formulas change (§5.2).
- Rate limiting: retries with exponential backoff and idempotent upserts (§5.1).
- Backfill command for N days (§12.2); `sync_intervals` job skeleton hooking into the Celery worker (full scheduling behavior is Feature 10).
- DB models for `activity`, `activity_stream` (per-second data or parquet blobs referenced from the row), `wellness` and raw file path fields (§6 data model minimum), with Alembic migrations.
- Storage of Intervals.icu's own load metrics solely as cross-check values (§5.1) if present, clearly marked as not-authoritative.

## Constraints

- **No `garth` / `python-garminconnect` / any unofficial Garmin library** (§5.1). All data comes via Intervals.icu only.
- Intervals.icu's own load metrics are **not consumed as truth**; our engine computes everything from raw streams. Intervals values may be stored only for cross-checking in tests (§5.1, and Feature 3 acceptance cross-check).
- **No Strava API data anywhere in this feature** (§5.3); Strava is excluded from v1 entirely.
- Respect Intervals.icu rate limits; retries with exponential backoff; upserts must be idempotent (§5.1, §12.2 acceptance).
- Secrets (personal API key) only via environment variables, never committed (§14).
- Units: metric; timezone: Europe/Madrid (§14).
- Ingestion may do I/O — the purity constraint applies to `app/engine/`, not `app/ingest/` (§6); ingestion must not call engine functions to compute final metrics here (metric computation is Feature 3's scope); storing raw/summary fields it receives is fine.
- Depends on Feature 1 (`project-scaffold`): stack, DB layer, Docker services (postgres, redis, worker) must exist.

## Checklist

- [x] `ING-1`: RED: write unit tests for the Intervals.icu client (auth header shape with `API_KEY` username, pagination/date-range params, rate-limit retry with exponential backoff, error mapping) against recorded/mocked HTTP responses; confirm they fail before implementation.
- [x] `ING-2`: GREEN: implement the `httpx`-based Intervals.icu client in `backend/app/ingest/` (§5.1) with activities, streams, wellness and FIT download methods, verifying exact endpoint paths against the official API cookbook and noting verification in docstrings.
- [x] `ING-3`: RED+GREEN: test-first DB models and Alembic migrations for `activity`, `activity_stream`, `wellness` per the §6 data model (including source ids used for idempotency and raw file path); upsert repository functions tested to create-then-update without duplicates.
- [x] `ING-4`: RED+GREEN: test-first FIT parsing with `fitdecode` on a sample FIT file fixture: per-second streams and swim lengths extracted and stored (§5.2).
- [x] `ING-5`: RED+GREEN: test-first idempotent sync orchestration: re-running the same date range creates no duplicate activities/streams/wellness rows (§12.2 acceptance), Intervals-side load metrics (if stored) are flagged non-authoritative cross-check values only (§5.1).
- [ ] `ING-6`: Implement raw FIT storage (object storage or local volume per §5.2) behind a small storage interface so the engine can be re-run later; store the path on the `activity` row.
- [ ] `ING-7`: Implement the backfill command for N days (§12.2) with logging of counts per endpoint and clear failure reporting on partial syncs.
- [ ] `ING-8`: Add a Celery task entry point for the sync (used by the scheduler in Feature 10) and exercise it in the Compose worker locally.
- [ ] `ING-9`: Document Intervals.icu endpoint verification results and rate-limit behavior in a short ADR or docs note under `docs/adr/` (§6 layout).

## Acceptance criteria

From PROJECT_BRIEF.md §12.2:

- Backfill of 180 days completes; re-running creates no duplicates; streams stored for every activity with data.

Directly implied conditions:

- Ingestion reads only from Intervals.icu with the personal API key; no unofficial Garmin libraries are present in the dependency tree (§5.1).
- Raw FIT files (when available) are stored so the engine can be re-run when formulas change (§5.2).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

In progress (Feature 2/11) on branch `feat/intervals-ingestion`.

- ING-1/2: client RED→GREEN (13 tests), commit `9e3d114` — endpoints verified against official cookbook (forum threads 80090/609); original file endpoint returns gzip, `fit-file` is the always-FIT alternative.
- ING-3: models/migration/upserts RED→GREEN, commits `3b94069` (postgres loopback port for local tests), `443527f` — migration daa3ba6946b9 upgrade/downgrade/upgrade verified; DB tests run against compose Postgres (skip if unreachable), never SQLite.
- ING-4: FIT parsing RED→GREEN (20 tests; real bike fixture MIT-licensed, swim lengths via pure stub functions), full suite 47 passed, mypy strict clean.
- ING-5: idempotent sync orchestration RED→GREEN (6 tests: idempotence re-run, load column isolation, partial failures, pacing ≤10 req/s with injected clock), full suite 53 passed, mypy strict clean. FIT-first streams with streams-endpoint fallback; per-item failures never abort the run.

Commits: 9e3d114, 3b94069, 443527f, 12882b9, d362ae0, d362ae0, (ING-5 pending commit)
