<a href="https://nan.builders/?ref=built-with-nan">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="https://nan.builders/badge/built-with-nan-light.svg">
    <img width="266" src="https://nan.builders/badge/built-with-nan-dark.svg" alt="Built with NaN">
  </picture>
</a>

# TRI COACH

A personal, science-based triathlon coach delivered over WhatsApp. A deterministic
Python engine (numpy/pandas/scipy) computes load, zones, readiness and race
predictions; an LLM agent only explains those results and never invents numbers.
See [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md) for the full specification.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python dependency manager, Python 3.12)
- Docker (with Compose v2)
- GNU Make

## Quickstart

```bash
cp .env.example .env   # fill in secrets; POSTGRES_PASSWORD is required (never commit .env)
uv sync --directory backend
make up                # api, worker, beat, postgres (pgvector), redis
curl http://localhost:8000/health
make test
make lint
```

## Conventions

- Units: **metric** only.
- Timezone: **Europe/Madrid**.
- Secrets only via environment variables; `.env.example` documents all variables.
- Engineering discipline: every feature follows
  [Organic Driven Development](https://en.wikipedia.org/wiki/Test-driven_development) —
  RED before GREEN, and each feature has a specification document in `odd/tasks/`
  with its checklist, acceptance criteria, owner decisions and verification
  evidence.

## Current status

All of the brief's features are implemented and verified end-to-end against the
running stack, except the pending items below.

- **Engine** — load (bike NP/IF/TSS, run rTSS, swim sTSS, TRIMP/hrTSS, strength
  sRPE with owner-entered RPE, Banister fit), zones (CP/W′, CS/D′, VDOT, CSS,
  Friel HR tables, threshold-change proposals), readiness (HRV/rHR/sleep
  multi-signal rule, no composite score), intensity distribution and durability
  (EF, Pa:HR, long-session trend). All pure functions with hand-calculated
  reference tests.
- **Ingestion** — Intervals.icu client (activities, streams, wellness, FIT
  download; rate-limit backoff), FIT parsing, idempotent sync, backfill command,
  raw FIT storage behind a storage interface, owner-entered RPE ingest.
- **WhatsApp agent** — signed Twilio webhook with allowlist, Strands Agents SDK
  loop with turn/token limits and per-conversation budget, ten engine-backed
  tools with typed outputs (`computed_at`, `engine_version`, coverage),
  conversation memory with rolling summary, language mirroring (Spanish/English),
  ~1,000-character WhatsApp formatting, insufficient-data and medical-referral
  rules, Langfuse per-turn tracing.
- **Scheduled messages** — Celery beat: `sync_intervals` (30 min), `morning_brief`
  (configurable time, template-window rule for outside the 24 h free-form window),
  `post_activity_review` on new-activity ingestion, `weekly_report` on Sundays,
  readiness recompute.
- **Charts** — Performance Manager, weekly TSS, mean-maximal curve with CP fit,
  pace/HR/EF trends, time-in-zone, HRV trend, race-prediction breakdown;
  snapshot-tested PNG renderers with expiring authenticated access and
  `plot_metric` media URLs delivered over Twilio.
- **Race prediction** — Riegel + VDOT spread for runs, Martin et al. 1998 bike
  physics solver per course segment, CSS-based swim, run-off-the-bike slowdown
  factor, composite triathlon total with Monte Carlo uncertainty.
- **Evidence store** — front-matter paper loader, deterministic chunker, pgvector
  embeddings and retrieval, `search_evidence` tool; the agent reports explicitly
  when the store has no supporting evidence and shows no references (owner
  decision).
- **Workout library** — cycling TXT and gym parsers with canonical JSON
  fixtures and round-trip regression tests.

## Pending work

- **Evals & observability (Feature 11)** — the whole feature is open:
  golden-set schema and ≥ 40 deterministic cases, fixed DB fixtures, eval runner
  against the fake LLM adapter, citation-compliance and safety checks, CI hard
  gates, p95 latency / cost-per-turn tracking with regression gate, calibrated
  LLM-as-judge for tone/clarity, Langfuse dashboards, eval-suite docs.
- **Voice notes (`WA-10`)** — media download, STT via a configurable provider,
  transcript as user message; audio not stored after processing.
- **Sync Celery entry point (`ING-8`)** — a dedicated task entry point for the
  sync, exercised in the Compose worker (the scheduler currently wraps the sync
  through its own task).
- **Seed papers (dropped, `EV-7`)** — by owner decision the seventeen brief-listed
  papers are not curated; `evidence/papers/` is empty, so the evidence store runs
  with no curated content until that decision is revisited.

## Repository layout

```
backend/app/
  api/        FastAPI routers: health, charts, WhatsApp webhook
  agent/      Strands SDK adapter, system prompt, tool registry, memory, budget, tracing
  services/   DB-backed services bridging engine outputs and repositories
  tools/      Tool functions exposed to the agent + CLI entry points
  engine/     PURE functions only — no I/O, no DB, no network
  ingest/     Intervals.icu client, FIT parser, sync/backfill jobs
  charts/     matplotlib renderers -> PNG bytes, storage + expiring access
  evidence/   Paper loader, chunker, embeddings, pgvector retrieval
  scheduler/  Celery beat: sync, morning brief, post-activity review, weekly report
  db/         Models, repositories, Alembic migrations
  core/       Settings, env audit, logging
backend/tests/
  engine/     Unit tests with known reference values
  scheduler/  Beat schedule, job and template-window tests
  tools/      Tool and webhook tests
  evals/      Golden set + eval runners (to be built)
backend/data/exercises/   Canonical workout-library JSON fixtures
evidence/     Paper summaries (evidence/papers/, currently empty)
infra/        docker-compose.yml (api, worker, beat, postgres+pgvector, redis)
docs/         PROJECT_BRIEF.md, ADRs
odd/tasks/    ODD feature documents (spec + checklist + evidence per feature)
```



[![Built with NaN](https://nan.builders/badge/built-with-nan-compact.svg)](https://nan.builders/?ref=built-with-nan)
