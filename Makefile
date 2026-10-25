SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# pnpm comes from corepack when it is not on PATH; package.json pins packageManager.
PNPM ?= $(shell command -v pnpm >/dev/null 2>&1 && echo pnpm || echo corepack pnpm)
# Optional compose profile for `make up` (e.g. PROFILE=apps).
PROFILE ?=

.PHONY: help setup up down migrate seed fmt lint typecheck test test-ts e2e e2e-ui openapi placeholders check load

help: ## List available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-14s %s\n", $$1, $$2}'

setup: ## Preflight toolchain, install Python and Node deps, fetch models, generate audio assets
	uv run --no-project --python 3.12 python scripts/check_toolchain.py
	uv sync --all-packages
	$(PNPM) install
	scripts/fetch_models.sh
	uv run python scripts/generate_audio_assets.py

up: ## Start infra (PROFILE=apps also starts services)
	docker compose $(if $(PROFILE),--profile $(PROFILE),) up -d --wait

down: ## Stop infra and services
	docker compose --profile apps down

migrate: ## Apply database migrations
	uv run alembic -c packages/va_db/alembic.ini upgrade head

seed: ## Seed development data
	uv run python scripts/seed_dev.py

fmt: ## Format Python and TypeScript
	uv run ruff format .
	$(PNPM) -r --if-present run format

lint: ## Lint Python and TypeScript
	uv run ruff check .
	uv run ruff format --check .
	$(PNPM) -r --if-present run lint

typecheck: ## Type-check Python (mypy strict) and TypeScript
	uv run mypy
	$(PNPM) -r --if-present run typecheck

test: ## Python unit + integration tests with coverage gate
	uv run pytest -n auto --cov --cov-report=term-missing --cov-fail-under=85

test-ts: ## TypeScript tests
	$(PNPM) -r --if-present run test

e2e: ## Simulator end-to-end suite
	uv run pytest tests_e2e -m e2e

e2e-ui: ## Dashboard Playwright suite
	$(PNPM) --filter dashboard exec playwright test

openapi: ## Regenerate openapi/openapi.json
	uv run python scripts/export_openapi.py

placeholders: ## Placeholder/stub detector gate
	uv run python scripts/check_no_placeholders.py

check: lint typecheck placeholders test test-ts ## All quality gates
	uv run python scripts/check_openapi_drift.py
	uv run python scripts/check_spec_refs.py

load: ## 1 000 concurrent simulated calls load test
	scripts/load/run.sh
