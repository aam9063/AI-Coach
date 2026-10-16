# ODD Feature: charts

Status: pending | Feature 7 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the server-side chart renderers (PROJECT_BRIEF.md §10): pure matplotlib functions that turn engine data into phone-readable PNG images, storage with expiring access URLs, and the `plot_metric` tool so the agent can send charts as images on WhatsApp.

## Problem

The coach must "generate charts of performance metrics and send them as images on WhatsApp" (PROJECT_BRIEF.md §1). A visual Performance Manager or HRV trend is faster to read on a phone than a table of numbers, and the brief requires the rendering to stay deterministic and testable — same data in, same PNG out — so charts are as trustworthy as the engine numbers they visualize (§3, §10).

## Scope

- `backend/app/charts/` (§6 layout): matplotlib renderers → PNG bytes (§10), one function per chart type:
  - Performance Manager chart: CTL, ATL, TSB over time.
  - Weekly TSS stacked by sport.
  - Mean-maximal power curve with CP fit overlay.
  - Run pace/HR trend and EF trend.
  - Time in zone (3-zone) per week.
  - HRV ln rMSSD with baseline band.
  - Race prediction breakdown with uncertainty bars.
- Rendering conventions (§10): 1080 px wide PNG, readable on a phone, consistent palette, dark text on light background, units on axes, date on title.
- Chart storage and serving (§9.1, §12.7): served from an authenticated, expiring FastAPI route (§15 decision made by the owner, 2026-10-16: FastAPI route over object storage — keeps personal health data inside the owner's infrastructure, no new storage dependency).
- `plot_metric(kind, date_range, sport?)` tool (§9.3) returning chart id + public media URL, as a thin wrapper usable by the agent loop from Feature 6.

## Constraints

- Each chart function is **pure: data in, PNG bytes out** (§10) — no DB, no network inside the renderers.
- Charts are **snapshot-tested** (§10, §12.7 acceptance).
- Rendering conventions are fixed by §10: 1080 px wide, readable on a phone, consistent palette, dark text on light background, units on axes, date on title.
- Twilio needs a public URL for `media_url`; access must be short-lived/expiring or authenticated (§9.1) — personal health data stays in the owner's infrastructure (§14).
- Only the minimum data required goes into chart inputs (§14: minimum context rule applies to what leaves the infrastructure — rendering is local, but stored/served URLs must be access-controlled).
- Units: metric; timezone: Europe/Madrid (§14 — date axis labels and titles follow it).
- Depends on Features 1 (`project-scaffold`), 3–5 (engine data for CTL/ATL/TSB, zones, mean-maximal curve, EF, 3-zone, HRV baselines) and 8 (race prediction breakdown with uncertainty bars); the `plot_metric` tool is registered by Feature 6's tool registry.

## Checklist

- [x] `CH-1`: RED: snapshot-test scaffolding for chart renderers (canonical input fixtures, PNG snapshot storage and comparison, clear update procedure) with one failing first case (§10, §12.7).
- [x] `CH-2`: GREEN: implement the Performance Manager chart (CTL, ATL, TSB over time) as a pure function data-in/PNG-bytes-out with §10 conventions (1080 px, palette, units on axes, date on title); snapshot green.
- [x] `CH-3`: RED+GREEN: weekly TSS stacked by sport chart (§10).
- [x] `CH-4`: RED+GREEN: mean-maximal power curve with CP fit overlay, reusing the curve/fit outputs from Feature 4 (§10, §7.3).
- [x] `CH-5`: RED+GREEN: run pace/HR trend and EF trend charts (§10).
- [x] `CH-6`: RED+GREEN: time-in-zone (3-zone) per week chart (§10, §7.5).
- [x] `CH-7`: RED+GREEN: HRV ln rMSSD chart with baseline band (§10, §7.4).
- [x] `CH-8`: RED+GREEN: race prediction breakdown chart with uncertainty bars, consuming `{point, lower, upper, ...}` outputs from Feature 8 (§10, §7.7).
- [x] `CH-9`: Implement chart storage + expiring access: object storage with short-lived presigned URL or authenticated expiring FastAPI route (§9.1), aligned with the §15 storage decision.
- [x] `CH-10`: RED+GREEN: `plot_metric(kind, date_range, sport?)` tool returning chart id + public media URL as a typed Pydantic model with `computed_at`, `engine_version`, coverage info (§9.3).
- [x] `CH-11`: Verify a chart arrives as an image on WhatsApp end-to-end via Twilio `media_url` on the running stack (§12.7).

## Log

L1: owner asked (2026-10-16): "Creo que la feature 7 de las gráficas no esta implementada, puedes mirarlo y si es así comenzamos a hacerlo?" — feature was not implemented; work started on feat/charts in its own worktree.
L2: owner decided (2026-10-16), answering the §15 open decision: serve chart PNGs through an authenticated, expiring FastAPI route (not object storage presigned URLs).

## Acceptance criteria

From PROJECT_BRIEF.md §12.7:

- Each chart arrives as an image on WhatsApp; snapshot tests pass.

Directly implied conditions:

- All chart functions are pure (data in, PNG bytes out) and follow the §10 rendering conventions (1080 px wide, units on axes, date on title, readable on a phone).
- Served chart URLs are short-lived/authenticated, not publicly permanent (§9.1).

## Verification evidence

- CH-1/CH-2, branch `feat/charts`: RED observed first (``ModuleNotFoundError: app.charts.performance_manager``); the missing-baseline path fails loudly with update instructions before any baseline is committed; after ``CHART_SNAPSHOT_UPDATE=1`` regeneration the suite is green on a plain re-run (byte-identical render, deterministic).
- Conventions checked: PNG width 1080 asserted explicitly (``test_is_1080_px_wide_per_rendering_conventions``), title carries the data's date range, units on the y-axis, empty input rejected with ``ValueError`` (never an empty chart).
- ``uv run ruff check app/charts tests/charts`` and ``uv run mypy app/charts tests/charts`` clean.

- CH-3, same branch: RED observed (``ModuleNotFoundError: app.charts.weekly_tss``); missing-baseline path fails loudly; after regeneration the suite is green on plain re-runs (8 tests). Extra pins: negative TSS rejected (``ValueError``), a sport appearing in only some weeks stacks with a consistent color. ruff + mypy clean.
- CH-9 (2026-10-16): delegated to a gentle-ai-worker; orchestrator ran the 9 DB-dependent tests against the live compose Postgres (they had been skipped in the worker's environment — root cause: missing `.env` in the worktree made settings fall back to defaults whose password does not match the container; injecting the real `DATABASE_URL` fixed it). Final: `uv run pytest tests/charts tests/api` 64 passed, ruff + mypy clean on the full tree (223 files). Design: `ChartRow` model (PNG bytes in the `chart` table — owner infrastructure §14), pure-DB `save_chart`/`load_chart_png`, stdlib HMAC-SHA256 `sign_chart_token`/`verify_chart_token` (no new dependencies), `GET /charts/{chart_id}?token=...` failing closed (403 expired/tampered/unconfigured secret; 404 unknown id with valid token), settings `chart_media_secret` + `chart_media_ttl_seconds` (600 s).
- CH-10 (2026-10-16): delegated to a gentle-ai-worker; orchestrator integrated. Kind registry: performance_manager, weekly_tss, time_in_zone, hrv (+ aliases ctl_atl, intensity); kinds without an engine data source answer status="error" naming the missing source (race_prediction -> Feature 8) — never fabricated. Fail-closed ordering: secret/public-base check before any DB read or render; media URL = {chart_public_base_url}/charts/{id}?token=... signed with chart_media_secret, ttl chart_media_ttl_seconds. Typed PlotMetricResult/ChartCoverage models with truthful coverage (window vs samples). Orchestrator fixes at integration: replaced the two obsolete TestPlotMetricStub tests (parent-owned file) with the new unavailable-kind contract (error responses may carry null chart fields; the invariant is nothing real is fabricated), and corrected test_sport_filter_and_no_data_weeks which seeded/queried non-canonical sport "bike" (the tool correctly rejects it; now "ride"). Verified with live Postgres: 49 agent tests passed, ruff + mypy clean (225 files).
- CH-9 (2026-10-16): delegated to five parallel gentle-ai-worker subagents with disjoint edit surfaces (renderer module + test file + snapshot per chart). Each observed RED first (ModuleNotFoundError), generated its baseline with CHART_SNAPSHOT_UPDATE=1, and verified a deterministic plain re-run. Orchestrator reviewed every renderer and every baseline PNG visually, then committed one work unit per chart plus an exports integration commit. Final integration state: `uv run pytest tests/charts` 41 passed, ruff clean, mypy clean (19 source files). All seven charts follow the §10 conventions (1080 px asserted per chart, units on axes, date range in the title, dark text on light background); race prediction has no date axis by contract and formats times as h:mm.

## Progress

In progress (branch `feat/charts`, Orca worktree `C:/Users/alber/orca/workspaces/AI-Coach/feature-7-charts`, isolated from the scheduled-messages session which owns the main checkout): CH-1..CH-8 done (all seven renderers). CH-1..CH-10 done. Remaining: CH-11 only — WhatsApp e2e via Twilio media_url on the running stack (blocked on owner decision: requires chart_media_secret + chart_public_base_url in the real .env, a stack restart shared with the scheduled-messages session, and one real WhatsApp message to the owner's phone). Race prediction chart (CH-8) is buildable as a pure renderer against canonical fixtures since Feature 8 is pending.

Commits: 7b230a5 (CH-1/CH-2) · 284c16e (docs) · 9dca0d2 (CH-3) · 8767193 (CH-4) · 69678fa (CH-5) · 0d738df (CH-6) · de5f5f6 (CH-7) · 68605ff (CH-8) · d62e087 (exports) · b3c5fc6 (CH-9) · e675b57 (CH-10) · 177140e (HEAD fix) · 5f68a26 (CH-11 docs) · 9815924 (legend redesign) · 13028c3 (legend docs)
