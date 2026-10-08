# ODD Feature: whatsapp-agent

Status: in progress | Feature 6 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

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

- [x] `WA-1`: RED: webhook security tests — request with invalid/missing `X-Twilio-Signature` is rejected; sender not in the allowlist is rejected; valid signature + allowlisted sender proceeds (§9.1, §12.6).
- [x] `WA-2`: GREEN: implement `POST /webhooks/whatsapp` with signature validation, allowlist check, fast ack and Celery task hand-off (§9.1).
- [x] `WA-3`: RED+GREEN: Celery task processes an inbound message end-to-end against a fake LLM adapter: reply sent via Twilio REST, `message_log` row written with tool calls and trace id (§6, §9.1).
- [x] `WA-4`: RED+GREEN: configure the **Strands Agents SDK** model (owner decision, see the SDK section below) — the OpenAI provider for real use and a **deterministic fake `Model`** for every test, so the suite never calls a provider. The provider stays switchable by configuration (one model object handed to the agent, per §6).
- [ ] `WA-5`: RED+GREEN: the tool-calling **loop is the SDK's**; our work is the guardrails around it — pass `limits=Limits(turns=…, total_tokens=…)` per invocation, enforce the per-conversation token/cost budget from `result.metrics.accumulated_usage`, and audit step/tool usage through hooks (`BeforeToolCallEvent` can even cancel a call). The tool registry still returns typed Pydantic models with `computed_at`, `engine_version` and data coverage (§9.2, §9.3).
- [ ] `WA-6`: RED+GREEN: implement engine-backed tools `get_load_status`, `get_zones`, `get_readiness`, `get_activity_analysis`, `get_intensity_distribution`, `log_subjective`, `propose_threshold_update` as thin wrappers over Features 3–5 (§9.3); `predict_race`, `plot_metric`, `search_evidence` wired to Features 8, 7, 9.
- [ ] `WA-7`: RED+GREEN: insufficient-data rule — a tool reporting insufficient data makes the agent state what is missing and how to get it (§9.3).
- [ ] `WA-8`: RED+GREEN: conversation memory (last N turns + rolling summary in DB) and system prompt with athlete context, metric units, language mirroring (Spanish/English) and WhatsApp formatting constraints (≤ ~1,000 chars, no markdown tables) (§9.2).
- [ ] `WA-9`: RED+GREEN: citation rule — replies containing scientific claims include at least one `(Author, Year)` citation; unsupported claims get an explicit "no evidence" statement (§3, §8); medical/pain signals trigger reduce-load + professional referral, never diagnosis (§3).
- [ ] `WA-10`: RED+GREEN: voice-note flow — media download, STT transcription via configurable provider, transcript treated as user message, transcript stored (audio not stored after processing) (§9.1).
- [ ] `WA-11`: Integrate Langfuse tracing per turn (prompt version, tool calls with inputs/outputs, tokens, cost, latency) through the SDK's own OpenTelemetry surface (`strands.telemetry`) exported over OTLP, rather than a hand-rolled tracer; prompt versions stored in repo (§13).
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

## Agent runtime: Strands Agents SDK (owner decision, 2026-10-07)

The agent is built on the **Strands Agents SDK** (`strands-agents`, Apache-2.0, Python 3.10+, 1.58.1 at the time of the decision) instead of a hand-rolled loop. A spike against the real package verified every extension point this feature depends on, so the implementation does not have to rediscover them:

| Need | Verified seam |
| --- | --- |
| Deterministic tests with no provider calls | A fake subclass of `strands.models.Model` runs the **real agent loop** (the spike exercised a tool-use turn plus a final answer, zero network). The loop only needs `stream`; `count_tokens`/`get_config`/`update_config`/`structured_output` complete the protocol. |
| Max steps | `Limits(turns=…, output_tokens=…, total_tokens=…)` passed **per invocation** (`agent(prompt, limits=…)`) — the constructor has no such parameter, which is the trap to avoid. |
| Token/cost budget | `result.metrics.accumulated_usage` (input/output/total tokens) and `accumulated_metrics` (latency) are returned per invocation; the per-conversation budget is ours to accumulate. |
| Conversation memory across Celery tasks | `strands.session` provides `SessionManager` and a **`RepositorySessionManager` + `SessionRepository`** pair, i.e. pluggable persistence — ours backed by the project's database, because each Celery task is a separate process. |
| Tracing to Langfuse | `strands.telemetry` (StrandsTelemetry/Tracer, OpenTelemetry-based) exported over OTLP; the exporter wiring is the part left to verify in WA-11. |
| Tools from our engine | `@tool` derives the schema from the function's **docstring and type hints** (`days: int = 7` became `{"type": "integer", "default": 7}`) and our tools may return Pydantic models. The docstring is what the model reads, so it is part of the behaviour. |
| Intervention | Hooks expose `BeforeToolCallEvent` with `cancel_tool`, plus `BeforeInvocationEvent`, `BeforeModelCallEvent`, `MessageAddedEvent`, and after-events carrying results. |

**Security constraint that comes with the SDK**: it can load tools from a directory (`load_tools_from_directory`) and there is a companion `strands-agents-tools` package with file-editing, shell and HTTP tools. The agent must be given an **explicit list containing only this project's engine tools** — never the directory loader, never the vended tools — otherwise the model could execute commands on the host.

## Progress

In progress (Feature 6/11) on branch `feat/whatsapp-agent`, branched from `dev` (which already carries Features 1-5, verification included — no stacking needed this time).

Owner decisions taken before starting (§15 and sequencing):
- **LLM provider: OpenAI** (the provider choice the brief requires before wiring a real one). The adapter stays provider-agnostic with a deterministic fake for tests, and the real provider is wired once a key exists.
- **Voice notes / STT: postponed** (WA-10 deferred) — the text path comes first.
- **Webhook exposure: a development tunnel**, not a deployment.
- Features 4-5 were built first precisely so this agent's `get_zones` / `get_readiness` tools wrap real engine outputs instead of stubs.

Environment gap (blocks only the live end-to-end, not the work): **no credentials are configured yet** — `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` and `TWILIO_WHATSAPP_ALLOWLIST` are all empty in the gitignored env files, and Docker had to be started by hand at the beginning of this session. So every checklist item is built test-first against deterministic fakes (mocked HTTP, a fake LLM adapter, eager Celery) and `WA-12`'s live verification waits for the owner's Twilio sandbox credentials, their allowlisted number, the OpenAI key and a running tunnel.

- WA-1/2 done at `b0b42c7`: `POST /webhooks/whatsapp` validates `X-Twilio-Signature` **unconditionally** (Twilio's documented HMAC-SHA1 over the signed URL plus the parameters sorted by key, constant-time compare) with **no switch anywhere to skip it**, checks the sender against a `whatsapp:`-tolerant allowlist, fast-acks 200 and hands the message to the Celery task, WARNING-logs every rejection without leaking the token, and fails closed when no auth token is configured. The router is registered on the real `create_app()` factory, guarded by a test that builds the app only through that factory. Parent-verified with a from-scratch HMAC: correct signature plus allowlisted sender → 200; missing signature, wrong token, non-allowlisted sender and missing token → 403 each; with the public URL configured, a local-URL signature → 403 and a public-URL signature → 200. **Operational detail hardened while verifying**: `TWILIO_PUBLIC_WEBHOOK_URL` must be the FULL URL including the `/webhooks/whatsapp` path — a bare host is used verbatim and rejects every real message — now stated in the settings field comment and both `.env.example` files. The owner's `TWILIO_VALIDATE_SIGNATURE` line was commented out by the parent (the app forbids unknown Settings keys) and no such toggle exists by design.

- WA-3/4 done at `92f5e16`: the Celery task processes an inbound message end-to-end. The inbound row is persisted **first** with a DB-level unique anchor on `message_sid`, so a Twilio retry hits the constraint and returns early — **no second agent run, no second reply**, even across restarts (Postgres admits many NULLs, so a conversation still holds many outbound/tool rows). Tool-call rows and the outbound row are committed **before** the send, so a failure costs at most a missed reply, never a duplicate. The agent runs on **Strands**: the SDK's own loop assembles messages, executes our tools and handles stop reasons, with the model chosen by configuration — OpenAI for production (constructed locally, no request) or a scripted fake for tests, and an unknown provider **raises** rather than falling back silently. The fake subclasses the SDK's `Model` and emits Bedrock-shaped chunks, so the tests exercise the real loop, with no network and no real credentials. Adds `message_log` (direction inbound/outbound/tool_call, JSONB payload, indexed `trace_id`, `agent_version` provenance) in a **new** migration, the `agent` package (pipeline, model factory, fake model, prompt and tool stubs), the dependencies (`strands-agents[openai]`, `twilio` with its untyped-import override) and the `LLM_PROVIDER`/`LLM_MODEL_ID` settings. Tool wrappers, the full prompt and the budget are stubs for WA-5/6/8/9.
- **Note on how this landed**: the first attempt died mid-cycle (pi exited with 0x40010004) leaving eight ruff/mypy failures and no report; the parent inspected the tree (nothing lost, the suite still collected) and a follow-up unit fixed the failures properly — missing and unused imports, import order, an async-generator return type and the `Settings(_env_file=...)` ignore the project already uses — instead of widening ignores. Parent-verified independently: a retry performs **0 model invocations and 0 sends** while the first delivery performs one of each with Twilio's addressing reversed correctly; a tool-use script drives **2** invocations through the real SDK loop and persists the `tool_call` row with its input and output; the factory raises on an unknown provider.

Commits: b0b42c7 (WA-1/2), 92f5e16 (WA-3/4)
