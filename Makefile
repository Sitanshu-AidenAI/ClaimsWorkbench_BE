# Convenience wrappers around the commands in README.md.
# Every target here is a shortcut, not a new source of truth.

.DEFAULT_GOAL := help
.PHONY: help install env up down logs migrate revision check lint format test test-integration \
        run worker beat keycloak build clean

CELERY := uv run celery -A app.workers.celery_app.celery_app

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (including dev extras)
	uv python install 3.13
	uv sync --extra dev

env: ## Create .env from the template if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env — review the host ports")

up: ## Start the infrastructure services
	docker compose up -d postgres redis minio minio-init keycloak wiremock mailpit

down: ## Stop all services
	docker compose down

logs: ## Tail service logs
	docker compose logs -f

migrate: ## Apply migrations
	uv run alembic upgrade head

revision: ## Create a migration: make revision m="add claim index"
	uv run alembic revision -m "$(m)"

check: lint test ## Lint and run the unit tests

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Format the codebase
	uv run ruff format .
	uv run ruff check --fix .

test: ## Run the unit tests
	uv run pytest -m "not integration"

test-integration: ## Run the integration tests (needs Postgres + Redis)
	uv run pytest -m integration

run: ## Run the API with reload
	uv run python main.py

worker: ## Run a Celery worker
	$(CELERY) worker --loglevel=info

beat: ## Run Celery beat
	$(CELERY) beat --loglevel=info

keycloak: ## Bootstrap the local Keycloak realm, clients and demo user
	KEYCLOAK_URL=http://localhost:$${CWB_KEYCLOAK_PORT:-8090} ./scripts/bootstrap-keycloak.sh

build: ## Build the application image
	docker build -t claims-workbench-api:local .

clean: ## Remove caches and build artefacts
	rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov coverage.xml .coverage
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
