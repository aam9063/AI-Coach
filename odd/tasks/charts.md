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
- Chart storage and serving (§9.1, §12.7): PNG uploaded to object storage with a short-lived presigned URL, or served from an authenticated, expiring FastAPI route — the choice aligned with the §15 open decision on object storage.
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

- [ ] `CH-1`: RED: snapshot-test scaffolding for chart renderers (canonical input fixtures, PNG snapshot storage and comparison, clear update procedure) with one failing first case (§10, §12.7).
- [ ] `CH-2`: GREEN: implement the Performance Manager chart (CTL, ATL, TSB over time) as a pure function data-in/PNG-bytes-out with §10 conventions (1080 px, palette, units on axes, date on title); snapshot green.
- [ ] `CH-3`: RED+GREEN: weekly TSS stacked by sport chart (§10).
- [ ] `CH-4`: RED+GREEN: mean-maximal power curve with CP fit overlay, reusing the curve/fit outputs from Feature 4 (§10, §7.3).
- [ ] `CH-5`: RED+GREEN: run pace/HR trend and EF trend charts (§10).
- [ ] `CH-6`: RED+GREEN: time-in-zone (3-zone) per week chart (§10, §7.5).
- [ ] `CH-7`: RED+GREEN: HRV ln rMSSD chart with baseline band (§10, §7.4).
- [ ] `CH-8`: RED+GREEN: race prediction breakdown chart with uncertainty bars, consuming `{point, lower, upper, ...}` outputs from Feature 8 (§10, §7.7).
- [ ] `CH-9`: Implement chart storage + expiring access: object storage with short-lived presigned URL or authenticated expiring FastAPI route (§9.1), aligned with the §15 storage decision.
- [ ] `CH-10`: RED+GREEN: `plot_metric(kind, date_range, sport?)` tool returning chart id + public media URL as a typed Pydantic model with `computed_at`, `engine_version`, coverage info (§9.3).
- [ ] `CH-11`: Verify a chart arrives as an image on WhatsApp end-to-end via Twilio `media_url` on the running stack (§12.7).

## Acceptance criteria

From PROJECT_BRIEF.md §12.7:

- Each chart arrives as an image on WhatsApp; snapshot tests pass.

Directly implied conditions:

- All chart functions are pure (data in, PNG bytes out) and follow the §10 rendering conventions (1080 px wide, units on axes, date on title, readable on a phone).
- Served chart URLs are short-lived/authenticated, not publicly permanent (§9.1).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

Not started.

Commits: (none yet)
