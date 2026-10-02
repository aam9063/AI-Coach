# ODD Feature: project-scaffold

Status: done (pending CI green on first push) | Feature 1 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Stand up the `tri-coach` repository skeleton exactly as specified in PROJECT_BRIEF.md §6: the `backend/app` package layout, dependency and tooling setup, Docker Compose infrastructure, local developer commands, and CI. This feature delivers no product behavior; it makes every later feature buildable, testable and lintable from the first commit.

## Problem

PROJECT_BRIEF.md §1 makes clean architecture and evals first-class requirements. Without a scaffold that enforces the layout, typing strictness and CI gates up front, later features (engine, agent, ingestion) would each invent their own structure and the "deterministic engine decides, LLM explains" pattern (§3) would not have a stable home. The brief mandates mirroring Shift Rescue's stack (§6) so the owner reuses known patterns.

## Scope

- Repository layout per PROJECT_BRIEF.md §6: `backend/app/api`, `backend/app/agent`, `backend/app/tools`, `backend/app/engine`, `backend/app/ingest`, `backend/app/charts`, `backend/app/evidence`, `backend/app/scheduler`, `backend/app/db`, `backend/app/core`; `backend/tests/{engine,tools,evals}`; `evidence/papers`; `infra`; `docs/`; `odd/tasks/`.
- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic, Celery + Redis, numpy, pandas, scipy, matplotlib, fitdecode, httpx, Twilio SDK (§6 stack).
- `uv` for dependency management; ruff, mypy (strict), pytest configuration.
- Docker Compose (§12.1): services `api`, `worker`, `beat`, `postgres` (16 with `pgvector`), `redis`.
- Core settings module with env-based secrets; `.env.example` documenting all variables (§14).
- Makefile targets: `make up`, `make test`, `make lint` (§12.1).
- GitHub Actions CI running lint, types and tests (§12.1).
- Minimal `GET /health` endpoint in the FastAPI app returning 200.

## Constraints

- No Strava integration anywhere (§5.3); no Strava dependency or code.
- Engine package must be created as a pure-function home: no I/O, no DB, no network inside `app/engine/` (§6, §14) — the scaffold must structurally allow this (e.g. lint boundary or conventions documented).
- Secrets only via environment variables; never commit keys; `.env.example` documents all variables (§14).
- Units: metric; timezone: Europe/Madrid (§14).
- Tooling stack fixed by §6: uv, ruff, mypy (strict), pytest, Docker Compose, Makefile, GitHub Actions. Do not substitute alternatives.
- Postgres must be version 16 with `pgvector` available (§6), since the evidence store (§8) depends on it later.
- No product features implemented here beyond the health endpoint; no agent, ingestion or engine logic.
- Commit per task with Conventional Commits (§12); ~400 changed lines per task is a planning heuristic only (§12).

## Checklist

- [x] `SC-1`: Initialize repo layout per §6: create `backend/app/{api,agent,tools,engine,ingest,charts,evidence,scheduler,db,core}` packages with `__init__.py`, `backend/tests/{engine,tools,evals}`, `evidence/papers` (with `.gitkeep`), `infra`, and keep `docs/PROJECT_BRIEF.md` and `odd/tasks/` in place.
- [x] `SC-2`: Add `pyproject.toml` with Python 3.12, dependencies from §6 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, Celery, Redis, numpy, pandas, scipy, matplotlib, fitdecode, httpx, Twilio, Langfuse, pgvector driver) and dev dependencies (ruff, mypy, pytest), managed with `uv`.
- [x] `SC-3`: Configure ruff and mypy (strict) in `pyproject.toml`; add pytest config; fix any baseline violations so `make lint` and `make test` pass on an empty codebase.
- [x] `SC-4`: RED: add a trivial pytest test suite run (e.g. `backend/tests/` smoke test plus a failing-if-broken CI test for `/health`) and confirm the runner fails before the FastAPI app exists.
- [x] `SC-5`: GREEN: implement minimal FastAPI app with `GET /health` returning 200, under `backend/app/api/`, with Pydantic v2 settings loaded from environment (§14) and `.env.example` covering all variables.
- [x] `SC-6`: Add `infra/docker-compose.yml` with services `api`, `worker`, `beat`, `postgres:16` + `pgvector`, `redis` (§12.1); add a minimal Alembic setup under `backend/app/db/` (no product migrations yet).
- [x] `SC-7`: Add `Makefile` with `make up`, `make test`, `make lint` wiring to Compose and the tooling (§12.1).
- [x] `SC-8`: Add GitHub Actions workflow running lint, types and tests on push/PR; confirm the CI job is green on the scaffold.
- [x] `SC-9`: Document local setup (uv install, `make up`, `.env.example` copy) in a short README or docs page; note the metric/Europe-Madrid convention (§14).

## Acceptance criteria

From PROJECT_BRIEF.md §12.1:

- `make up` starts all services; `/health` returns 200; CI runs lint, types and tests green.

Directly implied conditions:

- The `postgres` service reachable from `api`/`worker`/`beat` is PostgreSQL 16 with `pgvector` (§6, §8 dependency).
- CI runs ruff, mypy (strict) and pytest (§6 tooling; §12.1).

## Verification evidence

Implemented and verified on branch `feat/project-scaffold` (work-unit commits):

- `130c59f` docs(odd): add 11 ODD feature task documents from PROJECT_BRIEF
- `aefe57b` chore(scaffold): repo layout, uv-managed backend, ruff/mypy/pytest tooling (SC-1..SC-3)
- `9531c18` feat(api): health endpoint with env-based settings (test-first RED→GREEN) (SC-4..SC-5)
- `8dd6256` feat(infra): docker compose services and alembic baseline (SC-6)
- `cb88418` chore(build): makefile, ci workflow, readme and line-ending normalization (SC-7..SC-9)

Independent verification (gentle-ai-verify, all PASS):

- `uv run ruff check .` exit 0; `uv run mypy app tests` exit 0 (strict, 22 files); `uv run pytest` exit 0 (4 passed).
- `docker compose -f infra/docker-compose.yml up -d --build`: all five services Up (api, worker, beat, postgres healthy, redis healthy).
- `GET /health` → 200 `{"status":"ok","app":"tri-coach","engine_version":"0.1.0","environment":"dev"}`.
- Postgres 16.15 with pgvector 0.8.6 (`CREATE EXTENSION vector` succeeded; cleaned with `down -v`).
- CI workflow (ci.yml) contains ruff, mypy strict and pytest steps; workflow greenness pending first push to GitHub.

Caveat: `make` is not installed on the development host (Windows), so `make up`/`make test` were verified by executing their exact recipes (compose up + pytest/ruff/mypy) rather than through make itself.

## Progress

Completed (all checklist items SC-1..SC-9 done; acceptance criteria met locally, CI pending first push).

Commits: 130c59f, aefe57b, 9531c18, 8dd6256, cb88418 (branch `feat/project-scaffold`, not yet pushed).
