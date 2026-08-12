# Claims Workbench — Backend

FastAPI service for Claims Workbench. Python 3.13, PostgreSQL 16, Redis 7,
Celery, Keycloak 25, S3-compatible object storage.

The authoritative stack definition is [foundational-tech-stack.md](foundational-tech-stack.md).
Design decisions and the reasoning behind them are in
[docs/architecture.md](docs/architecture.md).

---

## Quick start

### 1. Dependencies

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and
`pyproject.toml` only — a single `uv.lock` is the lockfile. There is no
`requirements.txt`.

```bash
uv python install 3.13
uv sync --extra dev
```

### 2. Configuration

```bash
cp .env.example .env
```

Settings are read from the environment with the `CWB_` prefix and validated by
`pydantic-settings` at startup. Nested groups use a double underscore, so
`CWB_POSTGRES__HOST` and `CWB_POSTGRES_HOST` both reach
`settings.postgres.host` (each group also declares its own prefix).

### 3. Infrastructure

```bash
docker compose up -d postgres redis minio minio-init keycloak wiremock mailpit
```

Host ports are deliberately off the defaults so the stack can coexist with other
projects on the same machine. Each is overridable — set `CWB_PG_PORT`,
`CWB_REDIS_PORT`, `CWB_KEYCLOAK_PORT`, `CWB_MINIO_PORT`, `CWB_MINIO_CONSOLE_PORT`,
`CWB_WIREMOCK_PORT`, `CWB_MAILPIT_SMTP_PORT`, `CWB_MAILPIT_UI_PORT` or
`CWB_API_PORT` before `docker compose up`.

| Service       | Host URL                    | In-network        | Credentials                 |
| ------------- | --------------------------- | ----------------- | --------------------------- |
| Postgres      | `localhost:5442`            | `postgres:5432`   | `claims` / `claims`         |
| Redis         | `localhost:6389`            | `redis:6379`      | —                           |
| Keycloak      | http://localhost:8090       | `keycloak:8080`   | `admin` / `admin`           |
| MinIO API     | http://localhost:9020       | `minio:9000`      | `minioadmin` / `minioadmin` |
| MinIO console | http://localhost:9021       | —                 | `minioadmin` / `minioadmin` |
| WireMock      | http://localhost:8091       | `wiremock:8080`   | —                           |
| Mailpit UI    | http://localhost:8035       | `mailpit:8025`    | —                           |
| Mailpit SMTP  | `localhost:1035`            | `mailpit:1025`    | —                           |

Containers reach each other by service name on the in-network ports; the host
ports above are only for your machine. Your `.env` therefore needs the host
ports — `CWB_POSTGRES_PORT=5442`, `CWB_REDIS_PORT=6389` — when running the API
outside Docker.

### 4. Migrations

```bash
uv run alembic upgrade head
```

### 5. Run

```bash
uv run python main.py
```

- API — http://localhost:8000
- Docs — http://localhost:8000/docs (disabled in staging/production)
- Liveness — http://localhost:8000/api/v1/health
- Readiness — http://localhost:8000/api/v1/health/ready
- Metrics — http://localhost:8000/metrics

Background jobs:

```bash
uv run celery -A app.workers.celery_app.celery_app worker -l info
uv run celery -A app.workers.celery_app.celery_app beat -l info
```

The whole stack, including API, worker and beat, runs with:

```bash
docker compose up --build
```

---

## Project layout

```text
.
├── main.py                  # entry point — outside app/, exposes `app`, runs uvicorn
├── app/
│   ├── main.py              # create_app() factory, lifespan, middleware wiring
│   ├── api/
│   │   ├── deps/            # shared route dependencies (auth, db)
│   │   └── v1/
│   │       ├── router.py    # aggregates feature routers
│   │       └── routes/      # one module per feature area
│   ├── core/                # config, logging, errors, security, middleware, metrics
│   ├── db/
│   │   ├── base.py          # declarative Base — Alembic's target_metadata
│   │   ├── session.py       # SQLAlchemy async engine + session factory
│   │   ├── pool.py          # asyncpg pool
│   │   └── migrations/      # Alembic environment and versions
│   ├── models/              # SQLAlchemy mapped models
│   ├── schemas/             # Pydantic request/response models
│   ├── repositories/        # data-access objects
│   ├── services/            # business logic, cache, storage, PDF
│   ├── integrations/        # outbound HTTP clients
│   ├── workers/             # Celery app, tasks, beat schedule
│   └── utils/
├── tests/{unit,integration}/
├── docker/                  # service init scripts and stubs
└── pyproject.toml
```

Feature code goes in `app/`. The root-level `main.py` stays the executable
entry point; production runs Uvicorn workers under Gunicorn:

```bash
gunicorn main:app -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py
```

---

## Data access

Two access paths coexist, and **which route dependency a handler takes is what
enforces the split**:

| Dependency        | Path                       | Use for                                                                 |
| ----------------- | -------------------------- | ----------------------------------------------------------------------- |
| `ConnectionDep` / `TransactionDep` | asyncpg + hand-written SQL | Performance-critical transactional data, documents, audit. Tuned queries (lateral aggregates, `unnest` bulk inserts, conditional upserts) where an ORM would be in the way. |
| `SessionDep`      | SQLAlchemy `AsyncSession`  | Reference and administrative CRUD, plus reporting aggregates.            |

```python
from app.api.deps.db import ConnectionDep, SessionDep


@router.get("/claims/{claim_id}/timeline")  # hot transactional read
async def timeline(claim_id: UUID, conn: ConnectionDep): ...


@router.get("/reference/perils")  # reference data
async def perils(session: SessionDep): ...
```

The mapped models in `app/models/` are also Alembic's `target_metadata`, which
is what makes `alembic check` and `--autogenerate` usable.

**Migrations stay hand-written SQL.** Use `--autogenerate` to *discover* the
diff, then rewrite the body as explicit `op.execute()` statements so the exact
DDL that runs in production is reviewable in the diff:

```bash
uv run alembic revision -m "add claim status index"     # hand-written (preferred)
uv run alembic revision --autogenerate -m "..."         # to discover a diff
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic check                                     # models vs. migrations — runs in CI
```

---

## Authentication

Keycloak issues tokens; this service verifies them in-app against the realm
JWKS (`pyjwt[crypto]`) and never calls introspection on the request path. The
JWKS is cached, and an unrecognised `kid` triggers a single refresh, so realm
key rotation is a non-event.

```python
from app.api.deps.auth import CurrentPrincipal, require_roles


@router.get("/claims")
async def list_claims(principal: CurrentPrincipal): ...


@router.post("/claims", dependencies=[Depends(require_roles("claims-admin"))])
async def create_claim(): ...
```

`GET /api/v1/meta/whoami` echoes the decoded principal — the quickest end-to-end
check that the auth chain is wired correctly.

### Local realm setup

Keycloak starts in dev mode with no realm. Create one once:

```bash
./scripts/bootstrap-keycloak.sh
```

That creates the `claims-workbench` realm, the `claims-workbench-api` client,
the `claims-adjuster` / `claims-admin` roles, and a `demo` / `demo` user.

---

## Errors

Every failure leaves the API in one shape, so the frontend has a single branch
to write:

```json
{
  "error": { "code": "not_found", "message": "…", "details": {} },
  "request_id": "0f8c…"
}
```

Raise the types in `app/core/errors.py` (`NotFoundError`, `ConflictError`,
`PermissionDeniedError`, …) rather than `HTTPException`; the registered handlers
take care of status codes and logging.

---

## Observability

- **Logging** — structlog. Console renderer locally, JSON in staging/production.
- **Request ids** — read from `X-Request-ID` or generated, bound to the logging
  context, and echoed back on the response.
- **Metrics** — `prometheus-fastapi-instrumentator` exposes `/metrics`.

---

## Quality gates

```bash
uv run ruff check .                 # lint  (line-length 100; E,W,F,I,UP,B,SIM,C4,RUF)
uv run ruff format .                # format
uv run pytest                       # unit tests
uv run pytest -m integration        # requires Postgres + Redis
uv run alembic check                 # migrations match the models
```

Integration tests are marked and excluded from the default unit run:

```bash
uv run pytest -m "not integration"
```

CI (`.github/workflows/ci.yml`) runs ruff, pytest, the migration check, and the
frontend's vitest suite.


## FNOL intake

The first-notice module is the front door to the claims book: every claim in this
system is created by `POST /api/v1/fnol/{reference}/create-claim`, and nothing
else creates one.

### The pipeline

`app.services.fnol.pipeline` runs the stages in the order a claims officer works
in, and each feeds the next:

```
read documents → extract → classify → match policy → check completeness
→ detect duplicates → assess severity, fraud and coverage → match catastrophe
→ summarise → raise exceptions → set status
```

Two economies are built in. One model call reads the whole notice; everything
after it is deterministic code over the extracted values. And each analysis
carries a fingerprint of its inputs, so a re-run with nothing changed skips the
call entirely.

No stage failure raises. An unreachable provider, an unreadable PDF or an empty
policy book each become an exception on the notice, which is what the officer
works from.

### Layers

| Layer | Package | Holds |
| --- | --- | --- |
| Domain | `app.domain` | Pure rules: scoring, thresholds, the state machine. No I/O. |
| Services | `app.services.fnol` | Orchestration, persistence, audit. |
| AI | `app.services.ai` | Provider clients. Knows nothing about claims. |
| Documents | `app.services.documents` | Validation, storage, text extraction. Reusable. |
| Repositories | `app.repositories` | All SQL. |
| Routes | `app.api.v1.routes` | Authorise, validate, delegate, map, commit. |

### Running without a model provider

Leave `CWB_AI_API_KEY` unset and the module runs on the deterministic readers in
`app.domain.heuristics`, which parse the labelled fields broker notifications
actually use. Every record they produce is stamped `provider="heuristic"`, so
nothing on screen can mistake it for a model's reading. This is the path the
tests run on, and the one a carrier falls back to during a provider outage.

### Seed data

```bash
uv run python -m app.db.seed --reset
```

Seven notifications, written as the sources they would arrive as and then run
through the real pipeline — so every screen state is reachable without anyone
constructing it by hand: a straight-through claim, a possible duplicate, a major
loss, a catastrophe match, a loss outside the policy period, a file with several
fraud indicators, and a notice too incomplete to do anything with.

### Authorisation

`fnol-officer`, `claims-manager` and `claims-admin` may work intake;
`claims-handler` and `loss-adjuster` may read it; assignment and triage overrides
are `claims-manager` and `claims-admin` only. Enforced on the server —
`tests/unit/test_fnol_permissions.py` asserts that the refusal happens before any
database access.
