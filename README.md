# tri-coach

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

## Repository layout

```
backend/app/
  api/        FastAPI routers: health, WhatsApp webhook, admin
  agent/      LLM adapter, system prompt, tool registry, conversation memory
  tools/      Tool functions exposed to the agent (thin engine + db wrappers)
  engine/     PURE functions only — no I/O, no DB, no network
  ingest/     Intervals.icu client, FIT parser, sync jobs
  charts/     matplotlib renderers -> PNG bytes
  evidence/   Paper loader, chunker, embeddings, pgvector retrieval
  scheduler/  Celery beat jobs: morning brief, weekly report, sync
  db/         Models, repositories, Alembic migrations
  core/       Settings, logging, security
backend/tests/
  engine/     Unit tests with known reference values
  tools/
  evals/      Golden set + eval runners
evidence/     Paper summaries (evidence/papers/)
infra/        docker-compose.yml (api, worker, beat, postgres, redis)
docs/         PROJECT_BRIEF.md, ADRs
odd/tasks/    ODD feature documents
```
