# Developer commands for tri-coach (PROJECT_BRIEF.md §12.1).
# Plain targets only: this file is also used from Git Bash on Windows.

.PHONY: up down test lint format lock sync

## Start all services (api, worker, beat, postgres+pgvector, redis)
up:
	docker compose -f infra/docker-compose.yml --env-file .env up -d --build

## Stop and remove all services
down:
	docker compose -f infra/docker-compose.yml --env-file .env down

## Run backend tests
test:
	cd backend && uv run pytest

## Run ruff and mypy (strict)
lint:
	cd backend && uv run ruff check .
	cd backend && uv run mypy app tests

## Auto-format backend code
format:
	cd backend && uv run ruff format .
	cd backend && uv run ruff check --fix .

## Update uv.lock after changing dependencies
lock:
	cd backend && uv lock

## Install/sync the environment from uv.lock
sync:
	cd backend && uv sync
