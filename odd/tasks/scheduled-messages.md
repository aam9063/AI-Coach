# ODD Feature: scheduled-messages

Status: pending | Feature 10 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the Celery beat scheduled jobs (PROJECT_BRIEF.md §11): Intervals.icu sync, morning brief, post-activity review and weekly report, plus the WhatsApp template handling required for messages sent outside the 24-hour free-form window.

## Problem

A coach that only reacts to messages is incomplete: the owner expects a proactive daily brief, an automatic review after each activity, and a weekly summary (PROJECT_BRIEF.md §1, §11). Scheduling must respect the WhatsApp business rules — free-form messages only within 24 h of the last user message; anything outside that window needs an approved template (§9.1) — and must run reliably in the owner's timezone.

## Scope

- Celery beat jobs under `backend/app/scheduler/` (§6, §11):
  - `sync_intervals`: every 30 min, plus on-demand via the `/sync` command; idempotent (§11).
  - `morning_brief`: daily at a configurable time (Europe/Madrid); readiness + today's suggested session adjustment + one chart if relevant; uses an approved WhatsApp template when outside the 24 h window (§11).
  - `post_activity_review`: triggered when a new activity is ingested; short analysis plus a request for RPE/sensations (§11).
  - `weekly_report`: Sundays; load summary, intensity distribution, threshold proposals, next week focus (§11).
- WhatsApp templates: approved templates for out-of-window scheduled messages (§9.1, §11, §12.10).
- Timezone handling: Europe/Madrid for all schedules (§11, §14).
- Content assembly reuses earlier features: readiness from Feature 5, session adjustment suggestions via the §7.4 warning rule, load/distribution summaries from Features 3 and 5, threshold proposals from Feature 4, charts from Feature 7, ingestion triggering from Feature 2.
- The `/sync` on-demand command path into the agent's message handling (§11).

## Constraints

- `sync_intervals` must be **idempotent** and run every 30 min (§11); re-running creates no duplicates (consistent with Feature 2's acceptance).
- Morning brief uses an approved WhatsApp template when outside the 24 h window; free-form only within 24 h of the last user message (§9.1, §11, §12.10 acceptance).
- Timezone: Europe/Madrid for all job schedules (§11, §14).
- Warning/suggestion content in the brief must come from the engine's multi-signal rule — a single signal (and ACWR alone) never triggers reduce-intensity suggestions (§7.4, §7.2).
- Any scientific claim in scheduled messages follows the same citation rule as the agent (§3, §8).
- Template content goes to Twilio for approval; templates must not embed health data beyond what the template placeholders require (§14: minimum context to the provider).
- Secrets via environment variables only (§14).
- Depends on Features 1 (`project-scaffold`, Celery + beat + Redis), 2 (`intervals-ingestion`, sync logic and activity events), 3–5 (engine data), 6 (WhatsApp sending, allowlist context, message logging), 7 (charts for the morning brief) and 9 (citations where claims appear).

## Checklist

- [ ] `SCH-1`: RED: schedule configuration tests — beat schedule declares `sync_intervals` every 30 min, `morning_brief` daily at a configurable Europe/Madrid time, `weekly_report` on Sundays (§11).
- [ ] `SCH-2`: GREEN: implement the Celery beat configuration in `backend/app/scheduler/` with Europe/Madrid timezone (§11, §14).
- [ ] `SCH-3`: RED+GREEN: `sync_intervals` job wrapping Feature 2's sync; idempotency asserted by running twice against the same fixture with no duplicates (§11, §12.10).
- [ ] `SCH-4`: RED+GREEN: on-demand `/sync` command path triggers the same idempotent job (§11).
- [ ] `SCH-5`: RED+GREEN: `morning_brief` job — assembles readiness (Feature 5), today's suggested session adjustment via the multi-signal warning rule, and one chart if relevant (Feature 7) into a ≤ ~1,000-character message (§9.2, §11).
- [ ] `SCH-6`: RED+GREEN: template-window rule — when the send time is outside the 24 h free-form window, the morning brief uses an approved WhatsApp template; inside the window it sends free-form (§9.1, §11, §12.10).
- [ ] `SCH-7`: RED+GREEN: `post_activity_review` triggered by new-activity ingestion — short analysis (Feature 3/5 outputs) plus a request for RPE/sensations, feeding `log_subjective` (§9.3, §11).
- [ ] `SCH-8`: RED+GREEN: `weekly_report` on Sundays — load summary, intensity distribution, threshold proposals, next week focus (§11).
- [ ] `SCH-9`: Add `.env.example` entries for configurable schedule times and template SIDs; document template approval steps for Twilio (§14, §11).
- [ ] `SCH-10`: End-to-end verification on the running stack: jobs fire on schedule in Europe/Madrid time and messages outside the 24 h window use templates (§12.10).

## Acceptance criteria

From PROJECT_BRIEF.md §12.10:

- Jobs run on schedule in Europe/Madrid time; messages outside the 24 h window use templates.

Directly implied conditions:

- `sync_intervals` runs every 30 min and is idempotent (§11).
- All four jobs from §11 exist: sync, morning brief, post-activity review, weekly report.

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

Not started.

Commits: (none yet)
