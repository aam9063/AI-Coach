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

- [x] `SCH-1`: RED: schedule configuration tests — beat schedule declares `sync_intervals` every 30 min, `morning_brief` daily at a configurable Europe/Madrid time, `weekly_report` on Sundays (§11).
- [x] `SCH-2`: GREEN: implement the Celery beat configuration in `backend/app/scheduler/` with Europe/Madrid timezone (§11, §14).
- [x] `SCH-3`: RED+GREEN: `sync_intervals` job wrapping Feature 2's sync; idempotency asserted by running twice against the same fixture with no duplicates (§11, §12.10).
- [x] `SCH-4`: RED+GREEN: on-demand `/sync` command path triggers the same idempotent job (§11).
- [x] `SCH-5`: RED+GREEN: `morning_brief` job — assembles readiness (Feature 5), today's suggested session adjustment via the multi-signal warning rule, and one chart if relevant (Feature 7) into a ≤ ~1,000-character message (§9.2, §11).
- [x] `SCH-6`: RED+GREEN: template-window rule — when the send time is outside the 24 h free-form window, the morning brief uses an approved WhatsApp template; inside the window it sends free-form (§9.1, §11, §12.10).
- [x] `SCH-7`: RED+GREEN: `post_activity_review` triggered by new-activity ingestion — short analysis (Feature 3/5 outputs) plus a request for RPE/sensations, feeding `log_subjective` (§9.3, §11).
- [x] `SCH-8`: RED+GREEN: `weekly_report` on Sundays — load summary, intensity distribution, threshold proposals, next week focus (§11).
- [x] `SCH-9`: Add `.env.example` entries for configurable schedule times and template SIDs; document template approval steps for Twilio (§14, §11).
- [x] `SCH-10`: End-to-end verification on the running stack: jobs fire on schedule in Europe/Madrid time and messages outside the 24 h window use templates (§12.10).

## Acceptance criteria

From PROJECT_BRIEF.md §12.10:

- Jobs run on schedule in Europe/Madrid time; messages outside the 24 h window use templates.

Directly implied conditions:

- `sync_intervals` runs every 30 min and is idempotent (§11).
- All four jobs from §11 exist: sync, morning brief, post-activity review, weekly report.

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

**SCH-5/SCH-6 done at `08c2a68`** -- the brief and the window rule, which together carry the section 12.10 acceptance criterion. The composer is pure and reads PERSISTED engine data; the reduce-load suggestion appears ONLY when the persisted readiness snapshot says the engine's multi-signal rule fired, and **ACWR cannot enter the brief at all because `BriefData` has no such field** -- the constraint is structural, not a promise. The brief is under 1,000 characters, carries no citations and no chart (the owner's 2026-10-14 decision, and Feature 7's absence), and reuses the 24 h free-form window `budget.py` already defines (`FREEFORM_WINDOW = CONVERSATION_WINDOW`, identity-pinned: one definition, not two). Inside the window it sends free-form; outside it uses the configured Twilio template; **with no template SID configured it SKIPS loudly** (log + task summary + an outbound audit row with `skip_reason`), which is the sandbox's real behaviour and the honest thing to report. Every send and every skip is audited BEFORE the Twilio call, and the boundary is pinned (exactly 24 h counts as outside).

Parent-verified: 58 passed across the scheduler and settings tests, ruff clean, mypy strict clean (205 files), a single migration head, and the beat now declaring three entries -- the daily sync, the load recompute and `morning-brief` at 07:30 Europe/Madrid. **Recorded risks**: the template's placeholder variables are unverified against a real approved template (SCH-9/SCH-10 territory), and the brief time is read once when the beat app is created, so changing it needs a beat restart.

**SCH-1..SCH-4 done at `96ec872`**: the sync runs **every 30 minutes** (the brief's cadence) with the load recompute at :25 and :55, and the reasoning is recorded in the module -- the sync is idempotent and the client is paced, so the cadence's only cost is API calls while the numbers stay at most half an hour behind. The recompute runs after EVERY sync, and the recorded reason **refutes the alternative**: recomputing "only when a sync brought new activities" needs change detection the codebase does not have, because the upsert path counts re-syncs as `activities_synced`; the recompute is idempotent, single-athlete and bounded to the same 30-day window, so this is bounded cheap work. **Deliberate deviation from SCH-1's wording**: no weekly-report and no post-activity beat entry were added, because those tasks do not exist yet (SCH-7/SCH-8) and scheduling a job that cannot run would be a lie; the tests pin that absence.

The idempotency proof runs the REAL task body twice against the same fixture on the dedicated test database and compares row counts and identities -- the task summary counts upserts while the database proves there are no duplicates, which is the distinction that matters. `/sync` is intercepted **after the MessageSid anchor** (so a Twilio retry of the command runs the sync at most once) and **before the token budget** (the command invokes no model, so the budget neither blocks it nor pays for it); matching strips surrounding whitespace and case-folds, and a message that merely CONTAINS the word goes to the agent. It triggers the same job the beat runs, audits the outbound row before the send, and answers honestly about failed windows. Zero model invocation is pinned with a fake model that raises if called.

**Open risk found and proven pre-existing**: roughly one full agent-app run in five shows a single DB-backed failure, a different test each time, never reproducible in isolation. The writer proved it is pre-existing by stashing its changes and reproducing the same failure on the unmodified code. It is worth a dedicated investigation because CI runs the suite unattended.

**SCH-7/SCH-8 done at `15db6e1`**: all four jobs of section 11 now exist and the beat carries **five** entries (`*/30` sync, `:25/:55` recompute, 07:30 brief, `:05/:35` review, Sunday 18:00 report). The review's trigger is defined honestly rather than literally: the sync cannot report newness, so it is an idempotent pass over a **48 h lookback** whose once-only marker **IS the outbound audit row** the discipline already requires -- no schema change, no migration -- and a deliberate skip marks the activity too, so the skip is the recorded outcome and the pass never re-attempts. "Reviewed once" is pinned by running the trigger twice and asserting one review. The weekly report fires over the **COMPLETED** ISO week, never the still-running one, reading persisted data only, and on threshold proposals it makes the call worth recording: it **surfaces** the pending proposals and **never derives** new ones, because a weekly aggregate proves nothing about a threshold and deriving one would fabricate exactly the unbacked number the brief forbids -- pinned by a test showing the proposal still pending and the profile unchanged after the send. It also refuses to copy the engine's caveat texts because they carry author-year citations, which the owner's 2026-10-14 decision keeps out of replies.

Parent-verified: 81 passed across the scheduler and settings tests, ruff clean, mypy strict clean (242 files), a single head, and the five beat entries read back with their crontabs. The six scheduled-message settings are documented in both `.env.example` files. **Recorded**: the dev merge brought Feature 7, so `plot_metric` is no longer a stub -- the full charts implementation is registered in its place -- leaving `predict_race` as the only delegated tool. **Open risk**: a review can land before that activity's daily-load row exists (the recompute runs at :25/:55); the message states the gap honestly instead of waiting, since the RPE request does not depend on the numbers.

**SCH-9/SCH-10 done at `bd05dfd`**: the template-approval steps are recorded at the end of this document, and the on-demand CLI is what makes the acceptance criterion verifiable at all -- waiting for a crontab would have left it unproven. `python -m app.scheduler.cli {sync,morning_brief,post_activity_review,weekly_report}` runs the EXACT function each Celery task body calls, and its `--help` states the section 9.1 rule. **Live evidence, all four jobs run against the real stack**: `sync` pulled 6 streams and 2 wellness records from Intervals.icu; the morning brief SKIPPED with "no readiness snapshot for 2026-10-16 ... never composed from invented data"; the weekly report SKIPPED with "outside the 24 h free-form window (9.1) and no WhatsApp template SID configured (WEEKLY_REPORT_TEMPLATE_SID); the sandbox has no approved templates, so no send is attempted"; both wrote the audit rows the discipline requires (`kind`, `mode`, `skip_reason`). The template branch itself cannot be exercised without an approved template, which is stated rather than glossed over.

**Gap found by the live run -- the next unit**: nothing schedules the readiness computation, so `readiness_snapshot` has no row for today and the morning brief can never send. The feature's headline job is blocked by a missing schedule entry, not by its own logic.

### Getting an approved WhatsApp template

1. In the Twilio Console open **Messaging -> Content Template Builder** and create a template on the **WhatsApp** channel; its body declares any variable placeholders it uses.
2. Submit it for WhatsApp approval. Approval is granted by Meta, not Twilio, and is not instant.
3. When approved, copy its **ContentSid** (it starts with `HX`) into `MORNING_BRIEF_TEMPLATE_SID`, `POST_ACTIVITY_REVIEW_TEMPLATE_SID` or `WEEKLY_REPORT_TEMPLATE_SID` and restart the worker/beat so the setting is read.
4. Templates must not embed health data beyond what their placeholders require (section 14), and the code passes the whole brief as a single variable.

**The WhatsApp sandbox has no approved templates at all**, so inside it the free-form path (within the 24 h window) is the only one exercisable: outside the window the jobs skip loudly, which is the correct behaviour rather than a bug.

Commits: 08c2a68 (SCH-5 + SCH-6)
