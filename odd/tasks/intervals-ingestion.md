# ODD Feature: intervals-ingestion

Status: complete | Feature 2 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

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
- [x] `ING-6`: Implement raw FIT storage (object storage or local volume per §5.2) behind a small storage interface so the engine can be re-run later; store the path on the `activity` row.
- [x] `ING-7`: Implement the backfill command for N days (§12.2) with logging of counts per endpoint and clear failure reporting on partial syncs.
- [ ] `ING-8`: Add a Celery task entry point for the sync (used by the scheduler in Feature 10) and exercise it in the Compose worker locally.
- [x] `ING-9`: Document Intervals.icu endpoint verification results and rate-limit behavior in a short ADR or docs note under `docs/adr/` (§6 layout).

## Acceptance criteria

From PROJECT_BRIEF.md §12.2:

- Backfill of 180 days completes; re-running creates no duplicates; streams stored for every activity with data.

Directly implied conditions:

- Ingestion reads only from Intervals.icu with the personal API key; no unofficial Garmin libraries are present in the dependency tree (§5.1).
- Raw FIT files (when available) are stored so the engine can be re-run when formulas change (§5.2).

## Verification evidence

Independently verified by `gentle-ai-verify` (read-only) on HEAD `d4bf33f`, 2026-10-02 — 11/11 items PASS, no blockers:

- Quality gates: `uv run ruff check .` clean; `uv run mypy app tests` strict clean (46 source files); `uv run pytest -q` 86 passed, 0 failed, 0 skipped (DB-touching tests ran against compose Postgres 16).
- Idempotence §12.2: `test_sync_is_idempotent_on_rerun` re-runs the same range and asserts identical counts and the same physical rows (activities/streams/wellness).
- Migration from a clean scratch database: `alembic upgrade head` applied `daa3ba6946b9` with no errors; `activity`, `activity_stream`, `wellness`, `alembic_version` created; scratch DB dropped afterwards.
- Compose: api/worker/beat/postgres/redis all up; `/health` returned 200 with `{"status":"ok",...}`; `celery inspect registered` in the running worker listed `app.scheduler.tasks.sync_intervals`.
- Raw FIT storage: inside the worker container as the non-root user `app`, `save`/`load` round-tripped a payload through `storage_from_settings()` at the mounted `/data/fit` volume.
- §5.1/§5.3: no `garth` / `garminconnect` / `python-garminconnect` / Strava in `backend/pyproject.toml`, `backend/uv.lock`, or any `backend/app` import.
- §14: `.env` files untracked (only `.env.example` templates are tracked, with `POSTGRES_PASSWORD=CHANGE_ME` and empty key fields); compose requires `POSTGRES_PASSWORD` with no default.
- §5.1: Intervals.icu load is stored only in the non-authoritative `activity.intervals_icu_load` column and read by no engine code.
- §6 purity: no `app.engine` imports under `app/ingest/` and no ingest/db/http imports under `app/engine/` (engine itself is Feature 3 scope).

**Live acceptance closed (2026-10-02, owner's API key):** after the string-id fix (`e79bcd9` on `fix/ingest-activity-id`), the real `--days 180` backfill completed with `180/180 windows ok; activities=26 (failed=0) streams=128 (skipped=54, failed=0) wellness=175 (failed=0)`. The same command re-run left the row counts byte-identical (activity 26, activity_stream 131, wellness 175), proving the §12.2 no-duplicates criterion against live data rather than mocks. Real-data shape: 26 activities 2026-04-05..2026-10-02 (Ride 17, WeightTraining 5, Walk 4), streams mostly heart-rate only (no power meter), `/activity/{id}/file` returned an uncompressed FIT.

## Progress

Complete (Feature 2/11) on branch `feat/intervals-ingestion`.

**Integration note (recorded 2026-10-02):** the owner merged PR #1 (`feat/project-scaffold` → `dev`, commit `ceebba0`). A local `git pull --tags origin dev` then merged `dev` into the feature branch (`d8de2a7`), and because the feature branch had been created with `git checkout -b <branch> origin/dev`, its upstream was `origin/dev` — so the subsequent push delivered these commits to `dev` instead of creating a `feat/intervals-ingestion` remote branch. **This feature therefore landed in `dev` without its own pull request.** Nothing was lost (all commits are in `dev`; merged-state suite: 86 passed, 0 conflicts, secret fix intact) and the local branch upstream has been unset to prevent a repeat. Process fix: feature branches must never track `origin/dev`; push them explicitly with `git push -u origin <branch>:<branch>` and confirm with `git ls-remote --heads origin`.

- ING-1/2: client RED→GREEN (13 tests), commit `9e3d114` — endpoints verified against the official cookbook (forum threads 80090/609); the original-file endpoint returns gzip and `fit-file` is the always-FIT alternative.
- ING-3: models/migration/upserts RED→GREEN, commits `3b94069` (postgres loopback port for local tests), `443527f` — migration `daa3ba6946b9` verified upgrade/downgrade/upgrade; DB tests run against compose Postgres (skip if unreachable), never SQLite.
- ING-4: FIT parsing RED→GREEN (20 tests; real MIT-licensed bike fixture, swim lengths via pure stub functions), commit `12882b9`.
- ING-5: idempotent sync orchestration RED→GREEN (6 tests: idempotence re-run, load-column isolation, partial failures, pacing ≤10 req/s with injected clock), commit `20fd547`. FIT-first streams with streams-endpoint fallback; per-item failures never abort the run.
- ING-6: raw FIT storage RED→GREEN (12 storage tests + 2 sync integration tests), commit `c2db5e1`: `RawFileStorage` protocol, `LocalVolumeStorage` (flat `<activity_id>.fit`, atomic temp+replace, traversal rejection), `NullStorage`; `activity.raw_file_path` persisted; compose `fit-data` volume at `/data/fit` for api+worker, pre-owned by the non-root user in the Dockerfile.
- ING-7: N-day backfill RED→GREEN (13 tests), commit `d2db60a`: exact N-day windows across month/year boundaries, per-window counts and logging, continue-after-failure, CLI exit codes 0/1/2.
- ING-8: Celery `sync_intervals` task RED→GREEN (6 eager-mode tests), commit `ce4708a`: owns the async lifecycle, returns a JSON summary; window failures stay in the summary while unexpected exceptions fail the task; registration proven in the Compose worker.
- ING-9: ADR `docs/adr/0001-intervals-icu-api-verification.md`, commit `8d00300`.

Known follow-ups (not blockers):

- A re-sync whose FIT download fails resets `raw_file_path` to NULL (full-row upsert semantics); preserving the prior path needs a partial-update repository path.
- Backfill uses per-day windows (~2 API calls/day, ~360 for the 180-day run, ~36s of pacing at 10 req/s) for failure granularity and cheap retries; a chunked-window option is a possible later improvement.

Commits: 9e3d114, 3b94069, 443527f, 12882b9, d362ae0, 20fd547, c2db5e1, 4658d45, d2db60a, 7cd8158, ce4708a, 8d00300, a2d4ed8, d4bf33f

## Defects found against the live API (2026-10-02)

Running the real 180-day backfill with the owner's API key exposed a contract bug that 86 mocked tests could not catch:

- **Activity ids are strings** (`i163428838`, verified for all 26 activities in the window; the API's own docs use `GET /api/v1/activity/i55751783/file`). The model declared `id: int`, so **every** real activity failed validation and the backfill could not complete. Fixed on branch `fix/ingest-activity-id`: `Activity.id` is `str` with an int→str coercion validator, the string id is threaded through client/sync/repository/storage, and a migration alters `activity.source_id` from `Integer` to `String(32)` preserving the unique anchor. Verified live afterwards: 26/26 activities validate.
- **The original file endpoint is not always gzipped**: `/activity/{id}/file` returned an uncompressed FIT (`b'.FIT'`, 35,828 bytes). The magic-byte gzip detection in the client already handled both cases — no change needed, and the assumption is now documented.

Both findings are recorded in `docs/adr/0001-intervals-icu-api-verification.md` context: mocked tests alone are not sufficient evidence for an external API contract.
