# ODD Feature: whatsapp-agent

Status: pending | Feature 6 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Build the WhatsApp channel and the LLM agent behind it (PROJECT_BRIEF.md §9): a Twilio webhook with signature validation and phone allowlist, Celery-based async processing, a provider-agnostic LLM adapter, the tool-calling loop over the engine, conversation memory, and Langfuse tracing. This is where "deterministic engine decides, LLM explains" (§3) becomes user-facing: the agent explains engine outputs and never invents numbers.

## Problem

The coach must "talk like a chat but reason like a sports scientist" (PROJECT_BRIEF.md §2): every number computed, every recommendation traceable. A raw LLM would invent figures; the agent must instead call tools that return engine outputs and explain them, with every scientific claim cited from the evidence store (§3, §8). Security matters for a personal health tool: only the owner's phone may interact, and every inbound request must be authenticated (§9.1).

## Scope

- Twilio WhatsApp channel (§9.1): inbound webhook `POST /webhooks/whatsapp` (sandbox OK for single-user dev); `X-Twilio-Signature` validated on every request; reject senders not in the allowlist; fast ack and processing in a Celery task; reply via Twilio REST.
- Voice notes (§9.1): download media, transcribe via configurable STT provider, treat transcript as the user message; store transcript, not audio, after processing.
- Images (§9.1): charts sent with `media_url`; PNG uploaded to object storage with a short-lived presigned URL, or served from an authenticated, expiring FastAPI route (full chart renderers are Feature 7).
- Agent design (§9.2): system prompt stating the core principle (§3), athlete context summary, metric units, reply language = user's language (Spanish or English), WhatsApp formatting constraints (short paragraphs, no markdown tables, max ~1,000 characters per message, split if longer); tool-calling loop with max step limit and per-conversation token/cost budget; conversation memory (last N turns + rolling summary in DB).
- Tool registry (§9.3) exposing the initial tool set as thin wrappers over engine (Features 3–5, 8) + db, all returning typed Pydantic models with `computed_at`, `engine_version` and data coverage info:
  - `get_load_status(date_range, sport?)`, `get_zones(sport)`, `get_readiness(date)`, `get_activity_analysis(activity_id | "last")`, `get_intensity_distribution(weeks, sport?)`, `predict_race(race_id | distance, target_date?)`, `plot_metric(kind, date_range, sport?)`, `search_evidence(query, k)`, `log_subjective(rpe?, fatigue?, soreness?, notes?)`, `propose_threshold_update(sport, new_value, evidence)`.
- Insufficient-data rule (§9.3): when a tool reports insufficient data, the agent says what is missing and how to get it (e.g. "do a 400/200 swim test to set CSS").
- Citations (§3, §8): every scientific claim in a reply must cite a source from the evidence store as `(Author, Year)`; with no supporting source, the agent says so explicitly instead of improvising.
- Safety (§3): not a medical professional; on pain/injury/illness signals reduce load suggestions and recommend a doctor or physio; never diagnose.
- Langfuse tracing for every turn (§13) with prompt version, tool calls with inputs/outputs, tokens, cost, latency.
- `message_log` DB model (§6): inbound/outbound messages, tool calls, trace id.
- LLM provider behind a thin adapter interface, OpenAI or Anthropic, switchable by config (§6).

## Constraints

- The LLM **never calculates or estimates a performance number on its own**; it calls tools that return engine outputs, then explains them (§3).
- Validate `X-Twilio-Signature` on every request; reject any sender not in the allowlist (§9.1).
- No invented numbers: `¿cómo estoy de forma?` must return CTL/ATL/TSB from the engine verified against the DB (§12.6 acceptance).
- Every scientific claim cited from the evidence store; if no source supports a claim, the agent says so explicitly (§3, §8).
- Free-form WhatsApp messages only within 24 h of the last user message; scheduled messages outside that window need an approved template (§9.1; enforced fully in Feature 10).
- Secrets (Twilio credentials, LLM keys, STT keys) only via environment variables (§14); only the minimum context required for a reply is sent to the LLM provider (§14).
- LLM provider and STT provider are open decisions — ask the owner before implementing (§15).
- WhatsApp formatting: short paragraphs, no markdown tables, max ~1,000 characters per message, split if longer (§9.2).
- Depends on Features 1 (`project-scaffold`), 3–5 (engine modules the tools wrap), and 8/9 for `predict_race` and `search_evidence` tool implementations (registry can stub-delegate until those features land); `plot_metric` delegates to Feature 7.

## Checklist

- [ ] `WA-1`: RED: webhook security tests — request with invalid/missing `X-Twilio-Signature` is rejected; sender not in the allowlist is rejected; valid signature + allowlisted sender proceeds (§9.1, §12.6).
- [ ] `WA-2`: GREEN: implement `POST /webhooks/whatsapp` with signature validation, allowlist check, fast ack and Celery task hand-off (§9.1).
- [ ] `WA-3`: RED+GREEN: Celery task processes an inbound message end-to-end against a fake LLM adapter: reply sent via Twilio REST, `message_log` row written with tool calls and trace id (§6, §9.1).
- [ ] `WA-4`: RED+GREEN: LLM adapter interface with a config-switchable provider and a deterministic fake implementation for tests (§6); ask the owner for the provider choice per §15 before wiring a real provider.
- [ ] `WA-5`: RED+GREEN: tool-calling loop with max step limit and per-conversation token/cost budget; tool registry returns typed Pydantic models with `computed_at`, `engine_version`, data coverage (§9.2, §9.3).
- [ ] `WA-6`: RED+GREEN: implement engine-backed tools `get_load_status`, `get_zones`, `get_readiness`, `get_activity_analysis`, `get_intensity_distribution`, `log_subjective`, `propose_threshold_update` as thin wrappers over Features 3–5 (§9.3); `predict_race`, `plot_metric`, `search_evidence` wired to Features 8, 7, 9.
- [ ] `WA-7`: RED+GREEN: insufficient-data rule — a tool reporting insufficient data makes the agent state what is missing and how to get it (§9.3).
- [ ] `WA-8`: RED+GREEN: conversation memory (last N turns + rolling summary in DB) and system prompt with athlete context, metric units, language mirroring (Spanish/English) and WhatsApp formatting constraints (≤ ~1,000 chars, no markdown tables) (§9.2).
- [ ] `WA-9`: RED+GREEN: citation rule — replies containing scientific claims include at least one `(Author, Year)` citation; unsupported claims get an explicit "no evidence" statement (§3, §8); medical/pain signals trigger reduce-load + professional referral, never diagnosis (§3).
- [ ] `WA-10`: RED+GREEN: voice-note flow — media download, STT transcription via configurable provider, transcript treated as user message, transcript stored (audio not stored after processing) (§9.1).
- [ ] `WA-11`: Integrate Langfuse tracing per turn: prompt version, tool calls with inputs/outputs, tokens, cost, latency; prompt versions stored in repo (§13).
- [ ] `WA-12`: End-to-end verification against the running stack: "¿cómo estoy de forma?" returns CTL/ATL/TSB verified against DB values; non-allowlisted number rejected; every turn has a trace (§12.6).

## Acceptance criteria

From PROJECT_BRIEF.md §12.6:

- Asking "¿cómo estoy de forma?" returns CTL/ATL/TSB from the engine (verified against DB) with no invented numbers; non-allowlisted numbers are rejected; every turn has a trace.

Directly implied conditions:

- Every inbound request has a validated `X-Twilio-Signature` and an allowlist check (§9.1).
- The agent never computes numbers itself; tools return engine outputs (§3, §9.3).
- Medical/injury signals reduce load suggestions and recommend a professional; never diagnose (§3).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

Not started.

Commits: (none yet)
