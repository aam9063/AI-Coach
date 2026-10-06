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

- [ ] `FI-1`: RED: client tests for the multipart upload against a mocked transport — request shape (field name, filename, content type), a 201 creation, a 200 all-duplicates outcome reported as *duplicate* and not as an error, error mapping for 4xx/5xx, and that the API key never appears in the body.
- [ ] `FI-2`: GREEN: implement the upload method on the Intervals.icu client with a typed result carrying the created activity ids and the duplicate/file counts.
- [ ] `FI-3`: RED+GREEN: CLI `python -m app.tools.upload_fit <path...>` that uploads each path, archives the bytes through the storage interface, and reports per file: created vs duplicate, path archived, and any error with its reason.
- [ ] `FI-4`: RED+GREEN: immediate analysis from the local bytes — parse the FIT, select the load method from the session's own data, compute the load and (where a modality is usable) the 3-zone split, and print them with `engine_version`; tests use a real fixture FIT and assert the numbers.
- [ ] `FI-5`: End-to-end verification against the running stack with the owner's real files: upload a real `.fit` (created), confirm the activity appears in Intervals.icu, re-upload the same file (reported duplicate, no second activity), and show the printed analysis.

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

Commits: (pending)
