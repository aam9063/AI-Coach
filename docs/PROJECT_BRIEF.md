# Tri Coach: Project Brief

> Working name: `tri-coach` (rename freely).
> Audience: the coding agent working on this repo under the gentle-ai ODD workflow.
> This brief is the single source of truth for product intent. Turn each feature in section 12 into its own `odd/tasks/<feature-name>.md` before implementing it. Do not start SDD artifacts unless explicitly asked.

---

## 1. Objective

Build a **personal, science-based triathlon coach** for one athlete (the owner). It is reached through **WhatsApp** and backed by a **deterministic sports-science engine** written in Python.

The coach must:

- Compute training load, fitness, fatigue and form for swim, bike, run and strength.
- Derive and keep training zones up to date (power, pace, heart rate, swim CSS).
- Track readiness (HRV, resting HR, sleep) against the athlete's own baseline.
- Forecast race times (sprint, olympic, 70.3, full distance) with uncertainty ranges.
- Generate charts of performance metrics and send them as images on WhatsApp.
- Answer questions in natural language, **always grounding numbers in the engine and claims in cited scientific evidence**.

Secondary objective: this is a portfolio project for an AI Engineer profile. Evals, observability and clean architecture are first-class requirements, not extras.

## 2. Problem

Generic chatbots invent numbers and give advice with no evidence behind it. Training platforms show metrics but do not converse or adapt. The owner wants a coach that **talks like a chat but reasons like a sports scientist**: every number is computed, every recommendation is traceable to a source.

## 3. Core design principle (non-negotiable)

**Deterministic engine decides, LLM explains.** This is the same pattern as the owner's previous project, Shift Rescue.

- All metrics, zones, loads and predictions are computed by pure, unit-tested Python functions in `engine/`.
- The LLM never calculates or estimates a performance number on its own. It calls tools that return engine outputs, then explains them.
- Every scientific claim in a reply must cite a source from the evidence store (section 8). If no source supports a claim, the agent says so explicitly instead of improvising.
- The agent is not a medical professional. On pain, injury or illness signals it reduces load suggestions and recommends seeing a doctor or physio. It never diagnoses.

## 4. Scope

### In scope (v1)

- Single user (the owner). Phone number allowlist.
- Data ingestion from Garmin through Intervals.icu (section 5).
- Science engine (section 7).
- WhatsApp agent with tool calling (section 9).
- Charts as PNG images sent on WhatsApp (section 10).
- Scheduled messages: morning brief and weekly report (section 11).
- Evidence store with citations (section 8).
- Observability with Langfuse and an eval suite in CI (section 13).

### Out of scope (v1)

- Multi-user, billing, onboarding flows for strangers.
- Writing workouts back to the watch.
- Full automatic training plan generation. v1 adjusts and advises on sessions; complete periodized plan generation is a later feature.
- Web dashboard (optional later; may reuse the React setup from Shift Rescue).

## 5. Data sources and constraints

### 5.1 Garmin (primary source, via Intervals.icu)

Facts that shape the design:

- Garmin's official Health/Activity API is only granted to legal entities, not individuals.
- The unofficial libraries (`garth`, `python-garminconnect`) broke when Garmin changed its auth flow in March 2026. **Do not use them.**
- Garmin's Connect IQ SDK is for building watch apps; the FIT SDK only parses files. Neither gives account data access.

**Decision:** the owner connects Garmin to **Intervals.icu** (official Garmin sync). The app reads everything from the Intervals.icu API using a personal API key.

Intervals.icu API essentials:

- Base URL: `https://intervals.icu/api/v1/`
- Auth: HTTP Basic, username is the literal string `API_KEY`, password is the personal key (Settings > Developer Settings).
- Endpoints in use (verify exact paths and params against the official Swagger/API cookbook before coding):
  - `GET /athlete/{id}/activities?oldest=YYYY-MM-DD&newest=YYYY-MM-DD`
  - `GET /activities/{id}/streams` (power, HR, speed, cadence, altitude, distance, time)
  - `GET /athlete/{id}/wellness?oldest=...&newest=...` (HRV, resting HR, sleep, weight)
  - Original FIT file download for an activity (find the exact path in the API cookbook)
- Respect rate limits; implement retries with exponential backoff and idempotent upserts.
- Intervals.icu also computes its own load metrics. **Do not consume them as truth.** Our engine computes everything from raw streams. Intervals values may be stored only for cross-checking in tests.

### 5.2 FIT files

- Download original FIT files when available and parse them with `fitdecode` for second-by-second streams and swim lengths.
- Store raw files (object storage or local volume) so the engine can be re-run when formulas change.

### 5.3 Strava (excluded from the AI path)

Strava's current API agreement prohibits using any data obtained via its API in AI models or similar applications, including prompts and RAG. **No Strava API data may ever reach the LLM.** v1 does not integrate Strava at all. If added later, it must be isolated from the agent and from any prompt.

## 6. Architecture

Mirror Shift Rescue's stack so the owner reuses known patterns.

```
Garmin ──(official sync)──▶ Intervals.icu ──(API key)──▶ Ingestion workers (Celery)
                                                             │
                                                   PostgreSQL 16 + raw FIT storage
                                                             │
                                          engine/ (pure Python: numpy, pandas, scipy)
                                                             │
WhatsApp ◀──▶ Twilio ◀──▶ FastAPI webhook ──▶ Agent (LLM + tools) ──▶ evidence store (pgvector)
                                                             │
                                          Langfuse traces + eval suite in CI
```

### Stack

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic.
- Celery + Redis (workers and beat scheduler).
- PostgreSQL 16 with `pgvector` for the evidence store.
- numpy, pandas, scipy for the engine; matplotlib for charts.
- `fitdecode` for FIT parsing; `httpx` for HTTP clients.
- Twilio WhatsApp API.
- LLM provider behind a thin adapter interface (OpenAI or Anthropic, switchable by config).
- Langfuse for tracing.
- Tooling: `uv`, ruff, mypy (strict), pytest, Docker Compose, Makefile, GitHub Actions.

### Repository layout

```
backend/
  app/
    api/            # FastAPI routers: whatsapp webhook, health, admin
    agent/          # LLM adapter, system prompt, tool registry, conversation memory
    tools/          # Tool functions exposed to the agent (thin wrappers over engine + db)
    engine/         # PURE functions only. No I/O, no DB, no network.
      load.py       # TSS variants, TRIMP, CTL/ATL/TSB, ACWR, Banister model
      zones.py      # CP, CS, CSS, FTP, VDOT, zone tables
      readiness.py  # HRV baseline, RHR, sleep flags
      durability.py # EF, aerobic decoupling
      predict.py    # Riegel, VDOT, bike physics, triathlon composite
      intensity.py  # time in zone, 3-zone polarization
    ingest/         # Intervals.icu client, FIT parser, sync jobs
    charts/         # matplotlib renderers -> PNG bytes
    evidence/       # loader, chunker, embeddings, retrieval
    scheduler/      # Celery beat jobs: morning brief, weekly report, sync
    db/             # models, repositories, migrations
    core/           # settings, logging, security
  tests/
    engine/         # unit tests with known reference values
    tools/
    evals/          # golden set + runners (section 13)
evidence/
  papers/           # one markdown summary per paper (section 8)
infra/              # docker-compose, postgres, redis
docs/
  PROJECT_BRIEF.md  # this file
  adr/              # architecture decision records
odd/tasks/          # ODD feature documents
```

### Data model (minimum)

- `athlete_profile`: thresholds and their history (FTP/CP, W', run CS and D', LTHR per sport, max HR, CSS, weight, bike CdA/Crr/mass, gender for TRIMP coefficients).
- `activity`: id, source, sport, start time, duration, distance, elevation, raw file path, summary metrics computed by our engine (NP, IF, TSS variant used, EF, decoupling, time in zone).
- `activity_stream`: per-second data (or parquet blobs referenced from the row).
- `daily_load`: date, TSS per sport, total TSS, CTL, ATL, TSB, ACWR.
- `wellness`: date, HRV (rMSSD), ln rMSSD, resting HR, sleep duration/score, weight.
- `race`: name, date, distance type, course data (elevation profile, expected temperature), goal.
- `prediction`: race id, created at, per-segment predicted times with lower/upper bounds, model version.
- `message_log`: inbound/outbound WhatsApp messages, tool calls, trace id.
- `evidence_chunk`: paper id, chunk text, embedding, citation string.

Every engine output persisted to the DB carries `engine_version` so results can be recomputed.

## 7. Science engine specification

All functions are pure, typed and unit-tested against hand-calculated or published reference values. Defaults must be configurable. Each module docstring lists its references.

### 7.1 Training load

**Bike, power-based TSS (Coggan):**

- NP = (mean(rolling_30s_mean(power)^4))^(1/4)
- IF = NP / FTP
- TSS = (duration_s × NP × IF) / (FTP × 3600) × 100

**Run, rTSS (pace-based):**

- Use normalized graded pace (grade-adjusted speed) vs threshold run speed.
- IF = NGS / threshold_speed; rTSS = duration_h × IF² × 100.
- Grade adjustment model must be documented in the function docstring (reference the model chosen, e.g. Minetti et al. 2002 energy cost of gradient running).

**Swim, sTSS:**

- IF = normalized swim speed / CSS speed; sTSS = duration_h × IF³ × 100.

**Fallback when no power/pace is usable, HR-based TRIMP (Banister):**

- ΔHRr = (HRavg − HRrest) / (HRmax − HRrest)
- TRIMP = duration_min × ΔHRr × 0.64 × e^(1.92 × ΔHRr) (male coefficients; female 0.86 and 1.67; configurable)
- hrTSS = TRIMP / TRIMP_of_one_hour_at_LTHR × 100

**Strength:** session RPE method (Foster et al. 2001): load = RPE (CR-10) × minutes, scaled to TSS-equivalent with a documented, configurable factor. RPE is collected via WhatsApp after the session.

**Selection rule:** for each activity choose the best available method in this order: power, pace/speed, HR, sRPE. Persist which method was used.

### 7.2 Fitness, fatigue, form (Performance Manager model)

- CTL_t = CTL_{t-1} + (TSS_t − CTL_{t-1}) × (1 − e^(−1/42))
- ATL_t = ATL_{t-1} + (TSS_t − ATL_{t-1}) × (1 − e^(−1/7))
- TSB_t = CTL_{t-1} − ATL_{t-1}
- Time constants configurable. Computed on combined load and per sport.
- Seed CTL/ATL from at least 90 days of history; flag values as low-confidence before that.

**Banister impulse-response model (Banister et al. 1975):**

- p(t) = p0 + k1 × Σ w(s) e^(−(t−s)/τ1) − k2 × Σ w(s) e^(−(t−s)/τ2)
- Default τ1 = 42, τ2 = 7. Fit k1, k2 (and optionally τ) with `scipy.optimize` only when there are enough performance markers (races, tests). Report fit quality; never present an unfitted model as personalized.

**ACWR (EWMA version, Williams et al. 2017):**

- Acute 7-day and chronic 28-day EWMA ratio.
- Display as context only. It must **not** trigger warnings on its own, given the published criticism of ACWR as an injury predictor (Impellizzeri et al. 2020). Warnings combine multiple signals (section 7.4).

### 7.3 Zones and thresholds

**Bike, Critical Power (Monod & Scherrer 1965; Jones et al. 2019):**

- From the mean-maximal power curve, take best efforts between 2 and 20 minutes.
- Fit Work = CP × t + W' (linear form) and report CP, W' and fit error.
- FTP: configurable as manual value, CP-derived estimate, or 95% of best 20-min power. Always state which one is in use.
- Power zones (Coggan, % FTP): Z1 < 55, Z2 56 to 75, Z3 76 to 90, Z4 91 to 105, Z5 106 to 120, Z6 121 to 150, Z7 > 150.

**Run, Critical Speed:**

- Fit Distance = CS × t + D' from best efforts between roughly 3 and 20 minutes.
- VDOT (Daniels & Gilbert):
  - VO2 = −4.60 + 0.182258 × v + 0.000104 × v² (v in m/min)
  - %VO2max = 0.8 + 0.1894393 × e^(−0.012778 × t) + 0.2989558 × e^(−0.1932605 × t) (t in min)
  - VDOT = VO2 / %VO2max
- Training paces derived from VDOT tables (easy, marathon, threshold, interval, repetition).

**Swim, Critical Swim Speed (Wakayoshi et al. 1992):**

- CSS (m/s) = (400 − 200) / (T400 − T200), from time trials logged via WhatsApp or detected in pool sessions.
- Output pace per 100 m and swim zones relative to CSS.

**Heart rate (Friel, % LTHR), separate tables per sport:**

- Run: Z1 < 85, Z2 85 to 89, Z3 90 to 94, Z4 95 to 99, Z5a 100 to 102, Z5b 103 to 106, Z5c > 106.
- Bike: Z1 < 81, Z2 81 to 89, Z3 90 to 93, Z4 94 to 99, Z5a 100 to 102, Z5b 103 to 106, Z5c > 106.

**Threshold change detection:** when a new effort exceeds the model by a configurable margin, propose (not apply) an update via WhatsApp. The athlete confirms. Store history.

### 7.4 Readiness

- HRV: ln(rMSSD) daily, 7-day rolling mean vs 60-day baseline. Flag when the rolling mean falls outside baseline ± 0.5 SD (smallest worthwhile change approach; Plews et al. 2013, Kiviniemi et al. 2007).
- Resting HR: deviation vs 30-day baseline.
- Sleep: duration vs personal baseline.
- Readiness output is a structured object (each signal, its direction, its confidence), not a single magic score.
- Warning rule: suggest reducing intensity only when **two or more** signals agree (e.g. HRV below band + TSB very negative + subjective fatigue reported).

### 7.5 Intensity distribution

- Time in zone per sport per week.
- 3-zone model: Z1 below first threshold, Z2 between thresholds, Z3 above second threshold. Mapping from 5/7-zone tables documented in code.
- Report vs polarized (~80/20) and pyramidal distributions (Seiler 2010; Stöggl & Sperlich 2014). Describe, do not moralize.

### 7.6 Durability and efficiency

- Efficiency Factor: bike NP / avg HR; run NGS / avg HR.
- Aerobic decoupling (Pa:HR): (EF first half − EF second half) / EF first half. < 5% on long steady sessions suggests good aerobic durability (Friel).
- Track durability trends on long sessions (Maunder et al. 2021).

### 7.7 Race prediction

All predictions return `{point, lower, upper, method, assumptions, model_version}`. Never a bare number.

- **Run:** Riegel (1981): T2 = T1 × (D2/D1)^1.06, plus VDOT-based equivalent. Show both and their spread.
- **Bike:** physics model (Martin et al. 1998): P × η = [0.5 × ρ × CdA × v_air² × v] + [Crr × m × g × cos(θ) × v] + [m × g × sin(θ) × v]. Solve for v per course segment given a target power (fraction of CP/FTP chosen per distance). Inputs: CdA, Crr, total mass, air density (from temperature and altitude), drivetrain efficiency η (default 0.976), course gradient profile.
- **Swim:** pace from CSS with a distance-dependent fraction and an open-water adjustment factor (configurable, documented as an assumption).
- **Run off the bike:** apply a personal slowdown factor learned from bricks and past races. Until enough data exists, use a configurable prior and widen the uncertainty band. Do not hardcode an unsourced constant.
- **Transitions:** T1/T2 from past races or configurable defaults.
- **Triathlon total:** sum of segments; uncertainty propagated (Monte Carlo over input distributions is acceptable and preferred).

## 8. Evidence store

Purpose: let the agent cite sources for every scientific claim.

- `evidence/papers/<slug>.md`: one file per paper, written/curated by the owner (summaries in own words, not copied full text). Front matter: `title`, `authors`, `year`, `journal`, `doi`, `topics`, `key_findings`, `limitations`.
- Seed list (minimum): Banister 1975; Coggan (Training and Racing with a Power Meter); Foster 2001 (sRPE); Williams 2017 (EWMA ACWR); Impellizzeri 2020 (ACWR critique); Monod & Scherrer 1965; Jones 2019 (CP review); Wakayoshi 1992 (CSS); Daniels (Running Formula); Riegel 1981; Martin 1998 (cycling power model); Minetti 2002 (gradient running cost); Seiler 2010; Stöggl & Sperlich 2014; Plews 2013; Kiviniemi 2007; Maunder 2021 (durability).
- Ingest: chunk, embed, store in pgvector with the citation string.
- Retrieval tool returns chunks with citations. The agent must include at least one citation when making a scientific claim, formatted as `(Author, Year)` with DOI available on request.

## 9. WhatsApp agent

### 9.1 Channel

- Twilio WhatsApp (sandbox is fine for single-user dev). Inbound webhook: `POST /webhooks/whatsapp`.
- Validate `X-Twilio-Signature` on every request. Reject any sender not in the allowlist.
- Respond fast (ack) and process in a Celery task; send the reply via Twilio REST.
- Voice notes: download media, transcribe (configurable STT provider), treat transcript as the user message. Store transcript, not audio, after processing.
- Images: charts are sent with `media_url`. Twilio needs a public URL: upload PNG to object storage with a short-lived presigned URL, or serve from an authenticated, expiring FastAPI route.
- WhatsApp rules: free-form messages only within 24 h of the last user message. Scheduled messages outside that window need an approved template (section 11).

### 9.2 Agent design

- System prompt states the core principle (section 3), the athlete context summary, units (metric), language (reply in the language the user writes in: Spanish or English), and WhatsApp formatting constraints (short paragraphs, no markdown tables, max ~1,000 characters per message, split if longer).
- Tool calling loop with a max step limit and a per-conversation token/cost budget.
- Conversation memory: last N turns plus a rolling summary in DB.

### 9.3 Tools (initial set)

All tools return typed Pydantic models, including `computed_at`, `engine_version` and data coverage info.

| Tool | Returns |
|---|---|
| `get_load_status(date_range, sport?)` | daily TSS, CTL, ATL, TSB, ACWR, method used per activity |
| `get_zones(sport)` | current thresholds, zone table, source effort, last update |
| `get_readiness(date)` | HRV/RHR/sleep vs baseline with flags and confidence |
| `get_activity_analysis(activity_id \| "last")` | NP/IF/TSS, time in zone, EF, decoupling, notes |
| `get_intensity_distribution(weeks, sport?)` | 3-zone distribution per week |
| `predict_race(race_id \| distance, target_date?)` | per-segment and total prediction with bounds and assumptions |
| `plot_metric(kind, date_range, sport?)` | chart id + public media URL |
| `search_evidence(query, k)` | chunks with citations |
| `log_subjective(rpe?, fatigue?, soreness?, notes?)` | stored subjective entry |
| `propose_threshold_update(sport, new_value, evidence)` | pending proposal awaiting athlete confirmation |

Rule: if a tool reports insufficient data, the agent says what is missing and how to get it (e.g. "do a 400/200 swim test to set CSS").

## 10. Charts

Rendered server-side with matplotlib to PNG (1080 px wide, readable on a phone, consistent palette, dark text on light background, units on axes, date on title).

- Performance Manager chart: CTL, ATL, TSB over time.
- Weekly TSS stacked by sport.
- Mean-maximal power curve with CP fit overlay.
- Run pace/HR trend and EF trend.
- Time in zone (3-zone) per week.
- HRV ln rMSSD with baseline band.
- Race prediction breakdown with uncertainty bars.

Each chart function is pure (data in, PNG bytes out) and snapshot-tested.

## 11. Scheduled jobs (Celery beat)

- `sync_intervals`: every 30 min, plus on-demand via `/sync` command. Idempotent.
- `morning_brief`: daily at a configurable time (Europe/Madrid). Readiness + today's suggested session adjustment + one chart if relevant. Uses an approved WhatsApp template when outside the 24 h window.
- `post_activity_review`: triggered when a new activity is ingested. Short analysis plus a request for RPE/sensations.
- `weekly_report`: Sundays. Load summary, intensity distribution, threshold proposals, next week focus.

## 12. Features (create one `odd/tasks/<feature-name>.md` per item)

Each feature doc must include: objective, problem, scope, constraints, checklist with stable IDs, acceptance criteria, verification evidence, progress. Commit per task with Conventional Commits. ~400 changed lines per task is a planning heuristic only.

1. **`project-scaffold`**: repo layout, uv, Docker Compose (api, worker, beat, postgres+pgvector, redis), settings, ruff/mypy/pytest, Makefile (`make up`, `make test`, `make lint`), GitHub Actions CI.
   - Acceptance: `make up` starts all services; `/health` returns 200; CI runs lint, types and tests green.
2. **`intervals-ingestion`**: Intervals.icu client, activities + streams + wellness + FIT download, idempotent upserts, backfill command for N days.
   - Acceptance: backfill of 180 days completes; re-running creates no duplicates; streams stored for every activity with data.
3. **`engine-load`**: TSS variants, TRIMP, sRPE, method selection, CTL/ATL/TSB, ACWR, Banister fit.
   - Acceptance: unit tests with reference values for each formula; PMC values within an agreed tolerance of Intervals.icu for the same data (cross-check only).
4. **`engine-zones`**: CP/W', CS/D', CSS, VDOT, zone tables, threshold change detection.
   - Acceptance: fits validated on synthetic data with known parameters; zone tables match the published percentages.
5. **`engine-readiness-intensity-durability`**: HRV baseline logic, multi-signal warning rule, 3-zone distribution, EF, decoupling.
   - Acceptance: warning fires only when two or more signals agree (tested); decoupling matches hand-calculated example.
6. **`whatsapp-agent`**: Twilio webhook with signature validation and allowlist, Celery processing, LLM adapter, tool registry, conversation memory, Langfuse tracing.
   - Acceptance: asking "¿cómo estoy de forma?" returns CTL/ATL/TSB from the engine (verified against DB) with no invented numbers; non-allowlisted numbers are rejected; every turn has a trace.
7. **`charts`**: chart renderers, storage with expiring URLs, `plot_metric` tool.
   - Acceptance: each chart arrives as an image on WhatsApp; snapshot tests pass.
8. **`race-prediction`**: run, bike physics, swim, composite with Monte Carlo uncertainty, `predict_race` tool.
   - Acceptance: run predictions match Riegel/VDOT reference calculations; bike solver matches a hand-solved flat-course example; every prediction exposes bounds and assumptions.
9. **`evidence-store`**: paper markdown loader, chunking, embeddings, pgvector retrieval, `search_evidence` tool, citation rule in the prompt.
   - Acceptance: scientific claims in replies include at least one citation (checked by evals); a question with no supporting evidence gets an explicit "no evidence in store" answer.
10. **`scheduled-messages`**: sync job, morning brief, post-activity review, weekly report, WhatsApp templates.
    - Acceptance: jobs run on schedule in Europe/Madrid time; messages outside the 24 h window use templates.
11. **`evals-and-observability`**: golden set, eval runner in CI, Langfuse dashboards.
    - Acceptance: see section 13; CI fails if any threshold is not met.

## 13. Evals and observability

### Golden set (`tests/evals/golden.yaml`)

At least 40 cases, each with: user message, fixed DB fixture, expected tool calls, expected numeric values (from the engine) and tolerance, whether a citation is required, forbidden content.

Categories: load/form questions, zones, readiness, activity analysis, predictions, evidence questions, out-of-scope/medical questions, missing-data cases, Spanish and English variants.

### Metrics and CI thresholds

- **Numeric faithfulness:** every number in the reply matches a tool output within tolerance. Target 100%.
- **Tool selection accuracy:** expected tools called. Target ≥ 90%.
- **Citation compliance:** replies with scientific claims include a valid citation from the store. Target ≥ 95%.
- **Safety:** medical/injury prompts always recommend a professional and never diagnose. Target 100%.
- **Cost and latency:** p95 latency and cost per turn tracked; regression > 20% fails CI.

LLM-as-judge is allowed only for qualitative checks (tone, clarity) and must be calibrated against a small set of owner-labelled answers; report agreement rate.

### Observability

- Langfuse traces for every turn: prompt version, tool calls with inputs/outputs, tokens, cost, latency.
- Prompt versions stored in repo and referenced in traces.

## 14. Non-functional requirements and constraints

- Units: metric. Timezone: Europe/Madrid.
- Secrets only via environment variables; never commit keys. `.env.example` documents all variables.
- Personal health data stays in the owner's infrastructure. Only the minimum context required for a reply is sent to the LLM provider.
- No Strava API data in any prompt, embedding or tool output (section 5.3).
- Engine functions: no I/O, fully typed, docstring with formula and reference.
- Every assumption or default constant lives in config with a comment citing its source or marking it as an owner choice.

## 15. Open decisions (ask the owner before implementing the related feature)

- LLM provider and model (OpenAI vs Anthropic) and the STT provider for voice notes.
- Hosting (reuse Shift Rescue's AWS EC2 + Caddy setup vs cheaper alternative).
- Object storage for charts and FIT files (S3 vs local volume behind an authenticated route).
- Target races for v1 (name, date, distance) to seed the `race` table.
- Initial thresholds the owner already knows (FTP, LTHR per sport, CSS, max HR, weight, bike setup).
