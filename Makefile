# Convenience wrappers around the commands in README.md.
# Every target here is a shortcut, not a new source of truth.

.DEFAULT_GOAL := help
.PHONY: help install env up up-all down logs migrate revision check lint format test test-integration \
        dev run worker beat mail-intake keycloak build clean seed demo demo-reset demo-documents \
        eval-matching

CELERY := uv run celery -A app.workers.celery_app.celery_app

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (including dev extras)
	uv python install 3.13
	uv sync --extra dev

env: ## Create .env from the template if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env — review the host ports")

up: ## Start the infrastructure services (no app — use `make dev` for that)
	docker compose up -d postgres redis minio minio-init keycloak wiremock mailpit qdrant

up-all: ## Start everything in containers, app included (migrations run first)
	docker compose up -d --build migrate
	docker compose up -d --build api worker beat

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

eval-matching: ## Score the policy matcher against case_data/_ground_truth (no DB, no model)
	uv run python scripts/eval_policy_matching.py

dev: ## Run the whole backend: API + worker + beat, one Ctrl-C stops all
	./scripts/dev.sh

run: ## Run the API alone (no worker — nothing scheduled will happen)
	uv run python main.py

worker: ## Run a Celery worker alone
	$(CELERY) worker --loglevel=info

beat: ## Run Celery beat alone
	$(CELERY) beat --loglevel=info

mail-intake: ## Collect the shared Outlook mailbox once (needs the Graph settings)
	uv run python -m app.services.mail

seed: ## Load the development notifications and claims
	uv run python -m app.db.seed

demo: ## Run the default demo pack through the real pipeline end to end
	uv run python -m app.db.demo

demo-reset: ## Delete the demo case and run the default pack again from scratch
	uv run python -m app.db.demo --reset

demo-packs: ## Run every demo-data pack in turn: make demo-packs [PACK=slug]
	@if [ -n "$(PACK)" ]; then \
	  uv run python -m app.db.demo --pack "$(PACK)"; \
	else \
	  for pack in $$(uv run python -m app.db.demo --list-packs); do \
	    echo "── $$pack ──"; \
	    uv run python -m app.db.demo --pack "$$pack" || exit 1; \
	  done; \
	fi

demo-list: ## List the notification packs under demo-data/
	@uv run python -m app.db.demo --list-packs

demo-documents: ## Rebuild every demo pack's PDFs from their generators
	uv run python scripts/build_demo_documents.py
	uv run python scripts/build_demo_packs.py

keycloak: ## Bootstrap the local Keycloak realm, clients and demo user
	KEYCLOAK_URL=http://localhost:$${CWB_KEYCLOAK_PORT:-8090} ./scripts/bootstrap-keycloak.sh

build: ## Build the application image
	docker build -t claims-workbench-api:local .

clean: ## Remove caches and build artefacts
	rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov coverage.xml .coverage
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
