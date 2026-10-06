# ODD Feature: fit-intake

Status: in progress | Feature 12 (owner-requested, not in PROJECT_BRIEF.md §12) | Owner idea, 2026-10-07

## Objective

Let the owner hand the coach a `.fit` file and get it **stored, registered in the single data path, and analysed immediately** — today through a CLI, later through WhatsApp. It exists because the owner's iGPSPORT BSC500 does not expose its recordings for extraction, so files arrive by hand (a Strava user-data export, or the device's own export when available), and because the brief's single-source rule (§5.1) requires the activity to live in Intervals.icu rather than only in our database.

## Problem

Without an intake, a FIT file that is not already in Intervals.icu can never reach the engine: ingestion reads only from Intervals.icu (§5.1), and the BSC500's exports are not reaching it. The owner therefore has a three-month gap (after 2026-07-07) that no amount of engine work can fill. Manually uploading to Intervals.icu's Calendar page works but is a per-file chore, does not archive the raw file in our own storage (§5.2) and gives no immediate analysis.

## Scope (CLI first; the WhatsApp rail arrives with Feature 6)

- **Upload**: `POST /athlete/{id}/activities` (multipart form-data, field `file`) through the existing Intervals.icu client. Verified from the official cookbook: the endpoint accepts `.fit`, `.gpx`, `.fit.gz`, `.gpx.gz` or a zip, returns **201 when at least one activity was created and 200 when everything was a duplicate**, and **de-duplicates by a hash of the file contents** — so re-uploading the same file is safe and must be reported as a duplicate rather than an error.
- **CLI**: `python -m app.tools.upload_fit <path...>` — uploads each file, then for the ones that were newly created: archives the raw bytes locally through the ING-6 storage interface (§5.2), parses them with the engine's FIT parser, computes the session's load through the existing method selection, derives the session's intensity where a modality is usable, and prints a per-file summary (created/duplicate, load and method, zone split, anything skipped with its reason).
- **Provenance rule (explicit)**: the file is the owner's own recording. Our code **never talks to Strava's API and never will** (§5.3); the CLI takes a local path and uploads it to Intervals.icu. Nothing in this feature reads from Strava.
- **Analysis is per file, from local bytes**: the upload response identifies the created activity, but the analysis must not wait for a full sync round trip. The FIT bytes are already in hand, so the parser and the engine run on them directly, and the activity becomes visible to `daily_load`/intensity/durability on the next sync.
- **Deferred to Feature 6**: the WhatsApp inbound-media rail (Twilio signature validation, allowlist, media download, Celery processing). The core built here is what that rail will call.

## Constraints

- Secrets and endpoints unchanged: the upload uses the same client, base URL and Basic-auth API key as ingestion (§5.1, §14).
- The engine stays pure: parsing and load/intensity maths are already pure functions; the CLI and client do the I/O.
- Never silently ignore a duplicate: a 200 response is reported as "already present" so the owner can tell a no-op from a failure.
- Never silently drop a file: an unparsable or non-FIT file is reported with its reason.
- The activity must land in Intervals.icu, not only in our database: our database is derived, Intervals.icu is the source of record (§5.1).
- Intervals.icu's own computed values for the uploaded activity remain non-authoritative cross-checks (§5.1).

## Checklist

- [x] `FI-1`: RED: client tests for the multipart upload against a mocked transport — request shape (field name, filename, content type), a 201 creation, a 200 all-duplicates outcome reported as *duplicate* and not as an error, error mapping for 4xx/5xx, and that the API key never appears in the body.
- [x] `FI-2`: GREEN: implement the upload method on the Intervals.icu client with a typed result carrying the created activity ids and the duplicate/file counts.
- [x] `FI-3`: RED+GREEN: CLI `python -m app.tools.upload_fit <path...>` that uploads each path, archives the bytes through the storage interface, and reports per file: created vs duplicate, path archived, and any error with its reason.
- [x] `FI-4`: RED+GREEN: immediate analysis from the local bytes — parse the FIT, select the load method from the session's own data, compute the load and (where a modality is usable) the 3-zone split, and print them with `engine_version`; tests use a real fixture FIT and assert the numbers.
- [x] `FI-5`: End-to-end verification against the running stack with the owner's real files: upload a real `.fit` (created), confirm the activity appears in Intervals.icu, re-upload the same file (reported duplicate, no second activity), and show the printed analysis.

## Acceptance criteria

- A `.fit` handed to the CLI is registered in Intervals.icu **once** (re-uploads deduplicated by the platform's content hash), archived raw in our storage, and analysed immediately with engine-produced numbers — no invented values.
- The intake never touches Strava's API and documents that rule where the file's provenance is handled.
- Nothing is dropped silently: duplicates and failures are both reported with reasons.

## Verification evidence

To be filled when the feature is implemented.

## Progress

In progress on branch `feat/fit-intake`, **stacked on `feat/engine-readiness`** (which is itself stacked on `feat/engine-zones`; neither pull request is merged yet), because the immediate analysis reuses the intensity and load modules from those features.

Owner context: the iGPSPORT BSC500's recordings could not be extracted from the device, so the owner obtained `.fit` files through a Strava user-data export. The rule is recorded above: the file is treated as the owner's own recording, uploaded from a local path to Intervals.icu, and our code never integrates Strava's API (§5.3).

Related, not in scope: the owner's readiness inputs (HRV, resting HR, sleep) will arrive from a Garmin Forerunner 265 through the Garmin↔Intervals.icu connection once the Garmin wellness fields are enabled and the watch is worn overnight — a `.fit` activity file contains none of them.

- FI-1/2 done at `285cc50`: `upload_activity_file` on the existing Intervals.icu client, sharing its request/retry/exception plumbing, with a typed `ActivityUploadResult` distinguishing **created** (201, with the returned activity ids) from **duplicate** (200 — the platform de-duplicates by content hash, so a re-upload is safe and is reported as a duplicate rather than an error or an empty success). Parent-verified on a mocked transport: POST to `/api/v1/athlete/0/activities`, Basic auth, **the API key never in the body**, the file part named `file` with its filename, the optional `name`/`description` sent only when provided, 201 parsing the created ids, 200 reporting a duplicate with no retry, and 400/500 mapping to the client's typed errors.

- FI-3/4 done at `069aa18`: `python -m app.tools.upload_fit <path...> [--zip-batch N]` uploads each FIT, reports **created / duplicate / error / skipped with a reason in every case** (exit 1 on any failure, never a silent drop), archives the uploaded bytes keyed by the returned activity id (§5.2) and **analyses the created ones immediately from the local bytes** — sport, duration and streams from the FIT session message, load method selected from what the file actually carries, and the 3-zone split where a modality is usable, printed with `engine_version`. What a FIT cannot provide is documented rather than guessed (no RPE, no wellness, no thresholds). Owner-driven additions: `.fit.gz` inputs are uploaded as-is and decompressed only in memory for the analysis, directories are discovered **recursively**, and `--zip-batch N` batches uploads (skipping per-activity archiving when a multi-file batch's response cannot attribute ids to members — explicit and tested).
- **Live-API correction (`069aa18`)**: the upload response is a **JSON object in both 201 and 200** — `{icu_athlete_id, id, activities:[...]}` — not the array the cookbook documents, and a duplicate response carries the **existing** activity's id. The client parsing was rewritten around the observed shape with tolerant fallbacks and the mocked tests now encode the real shape; the CLI prints the risk that dedup matches only byte-identical files (a duplicate against a Garmin-sourced activity means the same ride sits in the account twice). The mocked tests had encoded the cookbook's wrong shape, which is exactly how the bug reached the CLI. Parent-verified with a fake client: created → archive + engine analysis (`method=hr`, `tss=0.18`, `modality=bike_hr`, Z1 `62.00 s`); duplicate → reported as already present with the risk note and no analysis; a typed 400 → per-file reason and exit 1.

- FI-5 verified against the live API on the owner's real files: (a) the **created** path was exercised with a real 2026-04-17 file — HTTP 201, the activity appeared in Intervals.icu (`i194265861`, then deleted; the account count returned to its previous value) — and (b) the **duplicate** path was exercised with one of the owner's already-uploaded files (`10570929919.fit.gz`), where the CLI reported `duplicate (already present as activity i194268270; nothing archived, no analysis)` with exit 0 and the account total stayed at 869, proving a re-upload is a no-op. The byte-identical-only caveat is printed on every duplicate, which is what makes the risk visible when the same ride arrived from another source.

Commits: Commits: 285cc50 (FI-1/2), 069aa18 (FI-3/4)
