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
│   ├── services/            # business logic, cache, storage, PDF, mailbox intake
│   ├── integrations/        # outbound HTTP clients (Microsoft Graph)
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

**There is exactly one credential this API accepts: a Keycloak-issued access
token, verified in-app against the realm JWKS (`pyjwt[crypto]`).** There is no
second token format and no development bypass — `app/core/devauth.py`, which
accepted unsigned `dev.<base64url(json)>` tokens, is gone. Introspection is never
called on the request path; the JWKS is cached, and an unrecognised `kid` triggers
a single refresh, so realm key rotation is a non-event.

```python
from app.api.deps.auth import CurrentPrincipal, require_roles


@router.get("/claims")
async def list_claims(principal: CurrentPrincipal): ...


@router.post("/claims", dependencies=[Depends(require_roles("claims-admin"))])
async def create_claim(): ...
```

`GET /api/v1/meta/whoami` echoes the decoded principal — the quickest end-to-end
check that the auth chain is wired correctly.

### How a browser gets one of those tokens

Two doors, and on neither of them is the SPA an OAuth client: it holds no client id
and never receives an authorization code.

**Email and password — the primary path.** The form is ours; the credential is
Keycloak's. `POST /auth/login` forwards it to the realm's token endpoint using the
direct access grant, and Keycloak verifies it against its own database. There is no
user table in `claims_workbench` and nothing reads one.

**Single sign-on — off for now.** `GET /auth/sso` redirects to the realm. The whole
path is built and tested, and `CWB_AUTH_SSO_ENABLED=false` is the only switch: it
gates the button *and* the route, so a hidden control never has a live URL behind it.
Turning it on also needs the client secret, which is why `/meta/config` reports the
two combined.

It stays in the tree because it is the only route that can carry MFA, a forced
password change, or federation to an external directory — the password grant has no
way to ask a second question.

```
POST /api/v1/auth/login     → {email, password} in, a session out
GET  /api/v1/auth/sso       → 307 to Keycloak (PKCE S256, state, nonce)
GET  /api/v1/auth/callback  → redeems the code, stores the refresh token,
                              sets an HttpOnly cookie, 303 into the SPA
POST /api/v1/auth/token     → cookie in, short-lived access token out
POST /api/v1/auth/logout    → destroys the grant, returns the end-session URL
```

Both doors converge: whichever grant produced the tokens, the refresh token goes to
Redis and the same `SessionResponse` comes back, so nothing above these routes can
tell which was used.

`POST /auth/login` is the only endpoint in this API that receives a password, and
three things follow. It is rate-limited per IP **and** per email
(`CWB_AUTH_LOGIN_MAX_ATTEMPTS`, default 10 per 300s) — Keycloak locks a single
account, but is blind to one source trying many accounts once each. Its refusal is
one generic sentence, because passing the realm's wording through would make it a
username-enumeration oracle; a lockout is the single exception, since somebody whose
correct password has stopped working needs to know why. And validation errors never
echo `input`, so a malformed body cannot return the password.


**The refresh token never reaches the browser.** It is held in Redis behind a
256-bit opaque handle, and the handle is what travels in the cookie
(`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/api/v1/auth`). The SPA holds its
access token in memory only — no `localStorage`, no `sessionStorage` — so a
reload re-mints rather than restoring a saved credential, and the worst an XSS can
do is act as the user while the tab is open.

Three properties are worth relying on, each covered by
`tests/unit/test_auth_session.py`:

* **Rotation.** Every refresh issues a new handle and retires the old one.
* **Reuse detection.** A retired handle presented again means two parties hold
  one cookie, so the whole grant family is destroyed and the person signs in
  again. This is what makes a captured cookie announce itself rather than work.
* **Two clocks.** `CWB_AUTH_IDLE_TIMEOUT_SECONDS` and
  `CWB_AUTH_ABSOLUTE_TIMEOUT_SECONDS`. A desk leaves tabs open all day, so an
  idle bound alone would never fire; an absolute bound alone would sign someone
  out mid-sentence.

CSRF is a concern for those two POSTs and nothing else — every other authenticated
route is bearer-only and carries no cookie, so it is not forgeable cross-site in
the first place. Both require an `Origin` in `CWB_CORS_ORIGINS` and an
`X-Requested-With` header, neither of which a cross-site form can set.

### Local realm setup

Keycloak starts in dev mode with no realm. Create one once:

```bash
docker compose up -d keycloak
./scripts/bootstrap-keycloak.sh
```

That creates the `claims-workbench` realm with brute-force protection and a
password policy, the confidential `claims-workbench-api` client (standard flow
enabled, since it is also what the API redeems codes with), an **audience mapper**
so tokens carry `aud: claims-workbench-api` rather than Keycloak's catch-all
`account`, the six realm roles in `app.domain.enums.Role`, and one user per role.

It prints the client secret. **Sign-in cannot work without it** —
`CWB_KEYCLOAK_CLIENT_SECRET` is no longer optional, and `/auth/login` says so
plainly rather than failing later at the token endpoint.

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

### Deleting a notification

`DELETE /api/v1/fnol/{reference}` removes a notice from every store that holds
any part of it — the case and its nine child tables, the passages, the dataset
runs and values, the attachment bytes in object storage, the vectors in the
search index, and the mailbox ledger rows that collected it. It answers with a
receipt: what was removed, from where, and anything that could not be.

Three rules make it safe to expose:

* **A converted notice is refused.** Once `create-claim` has run, the claim is
  the record and deleting the notice would leave it citing nothing. `409`, with
  the claim reference in the message.
* **The audit trail survives**, and gains a final `fnol.deleted` event naming who
  did it and why. Audit rows carry no foreign key to the case precisely so a
  deletion cannot reach them.
* **Postgres commits before the stores that cannot roll back.** A failure
  sweeping the bucket or the index is reported in `warnings`, not raised — the
  rows have already gone, and a `502` for a case that no longer exists helps
  nobody.

It is not a lifecycle transition. Rejecting or cancelling keeps the record and
keeps why, and is what an officer wanting a notice off the queue usually means;
this is for a notice that should never have existed. `app/services/fnol/deletion.py`
carries the reasoning; `tests/integration/test_fnol_deletion_flow.py` proves it
against a real database, where the `ON DELETE CASCADE`s actually run.

### Authorisation

`fnol-officer`, `claims-manager` and `claims-admin` may work intake;
`claims-handler` and `loss-adjuster` may read it; assignment and triage overrides
are `claims-manager` and `claims-admin` only. Deleting a notification is narrower
still — `claims-manager` and `claims-admin`, `FNOL_DELETE_ROLES` — because every
other write leaves the record standing. Enforced on the server —
`tests/unit/test_fnol_permissions.py` asserts that the refusal happens before any
database access.

---

## Outlook mailbox intake

Notifications are collected from a shared Outlook mailbox over Microsoft Graph,
turned into FNOL notices with their attachments stored as claim documents, and
left `queued` for the extraction pipeline. Full setup, permissions and
troubleshooting: **[docs/mail-intake.md](docs/mail-intake.md)**.

**Keycloak is not required for this flow.** The service reads the mailbox as
itself, under application permissions and the client-credentials grant — no user
signs in. Keycloak authorises the people who trigger a poll through the API;
Graph authorises the service that does the collecting.

```bash
# Four settings, and nothing else is required.
CWB_GRAPH_TENANT_ID=...          # or GRAPH_TENANT_ID
CWB_GRAPH_CLIENT_ID=...          # or GRAPH_CLIENT_ID
CWB_GRAPH_CLIENT_SECRET=...      # or GRAPH_CLIENT_SECRET
CWB_GRAPH_SHARED_MAILBOX=claims@carrier.example   # or OUTLOOK_SHARED_MAILBOX
```

Graph application permission: **`Mail.ReadWrite`** with admin consent
(`Mail.Read` suffices if `CWB_GRAPH_MARK_AS_READ=false`; `Mail.ReadBasic` never
does — it carries neither body nor attachments). Restrict the app registration to
the one mailbox with an application access policy.

```bash
make mail-intake                       # collect once, now
CWB_GRAPH_POLL_ENABLED=true make beat  # every CWB_GRAPH_POLL_INTERVAL_SECONDS
curl -X POST localhost:8000/api/v1/mail-intake/poll -H "Authorization: Bearer $TOKEN"
curl "localhost:8000/api/v1/mail-intake/messages?status=failed" -H "Authorization: Bearer $TOKEN"
```

Every message ever seen is a row in `mail_intake_messages`, and every
attachment — stored or refused, with the reason — a row in
`mail_intake_attachments`. Both the Graph message id and the RFC 5322
`Message-ID` are unique, so a re-delivered email produces no second notice. One
message failing does not stop the batch; one attachment failing does not lose
the message. Nothing is ever deleted from the mailbox, and moving messages is
opt-in.

---

## Notifications

**The desk finds out that an email arrived without watching the board.** A
collected message raises a notification in the same transaction as the notice it
became, and the pipeline raises one more when it starts and one when it finishes —
so the bell in the top bar counts arrivals, successes and failures. Full
reference: **[docs/notifications.md](docs/notifications.md)**.

No configuration; nothing here is tunable because nothing needed to be.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/notifications` | The panel — newest first, with a per-caller unread total |
| `POST` | `/api/v1/notifications/read` | Acknowledge what was shown |
| `POST` | `/api/v1/notifications/read-all` | Clear the badge |

Notifications are **desk-wide** — a broker's email arrives at the claims desk, not
at a person, and intake runs with no human in the loop to address it to. The
*read* state is **per-person**, keyed on the token subject in
`notification_reads`, so one officer clearing their badge does not clear it for the
rest of the desk. `dedupe_key` is unique in Postgres, which is what makes a
re-polled mailbox or a re-delivered Celery task produce no second row on anyone's
panel. Gated on `FNOL_READ_ROLES`: being told something is not a decision, so a
`claims-handler` may read and acknowledge where they may not process.

The frontend polls every 20s while signed in, never while the tab is hidden, and
never at all while signed out. Delivery is polling rather than a socket because
the mailbox behind it is itself polled every 300s — an arrival is minutes old
before anything could push it.

```bash
uv run pytest tests/integration/test_mail_intake_pipeline_flow.py   # the whole seam
```

---

## Extraction datasets

**What the system reads out of a notice is configuration, not code.** A dataset
is a named set of fields; each field carries a description written the way a
document phrases the thing, and that description is used verbatim as its
retrieval query. Adding a field is a row in a table.

Full reference — configuring one, what it costs, how it degrades, and the
frontend contract: **[docs/extraction-datasets.md](docs/extraction-datasets.md)**.

`FNOL notice` ships as the default and is seeded on boot. Its field keys are the
same dotted paths the review screen already uses, so its values are mirrored onto
the claim record by `app/services/fnol/adapter.py` — the one module that knows
both a dataset and a claim.

```bash
curl localhost:8000/api/v1/extraction/schemas -H "Authorization: Bearer $TOKEN"
curl -X PUT localhost:8000/api/v1/extraction/schemas/fnol_notice/fields \
  -H "Authorization: Bearer $TOKEN" -d '{"fields": [...]}'
curl -X POST localhost:8000/api/v1/fnol/FNOL-2026-000123/extraction \
  -H "Authorization: Bearer $TOKEN" -d '{"force": true}'
curl localhost:8000/api/v1/fnol/FNOL-2026-000123/extraction/values/policy.policy_number/evidence \
  -H "Authorization: Bearer $TOKEN"
```

The **notification body is a document**, written out alongside the attachments
and read, chunked, embedded and cited by exactly the same code. That is what
makes a value read out of the broker's own email point at a source rather than at
nothing.

Editing a dataset is gated on `claims-admin` / `business-admin`; running one and
correcting its values are ordinary intake writes. With no model provider
configured the dataset path stands aside entirely and the deterministic reader
runs, exactly as before.

---

## Extraction review, and the demo

The screen the two layers above exist for. `/intake/:reference` opens on
**extraction review**: the dataset's values on the left, and on the right the
page of the document each was read from, with the quoted text boxed on it.
Clicking a value asks the server where it came from; the citation is a passage id
recorded at extraction time and the rectangles are read off the stored PDF's word
geometry, so neither can be guessed in a browser. **Intelligence review** is one
click away and holds the pipeline's conclusions — completeness, severity, fraud
signals, coverage, duplicates and the exception list.

How it fits together, what to set, and what it does when a provider is missing:
**[docs/extraction-review.md](docs/extraction-review.md)**.

A full demo runs on committed, realistic documents — a two-page loss notice, a
certificate of insurance, a three-page marine survey, an itemised damage schedule
and the covering email:

```bash
make up && make migrate
make demo          # ingest, index, extract, and report on what landed
make demo-reset    # delete the demo case and run it again
```

It drives the same `build_pipeline` the API route and the Celery worker use, so
it cannot pass while production is broken, and it exits non-zero when the
pipeline did not produce what the screen needs. The document set and the scenario
behind it: **[demo-data/document-intelligence/](demo-data/document-intelligence/)**.

`CWB_AI_API_KEY` is what the citations depend on. Without it the documents are
still read, chunked and indexed, but the dataset path stands aside and the
deterministic reader — which never read a passage — cannot cite one. The demo
reports that rather than pretending.

### The policy library

An administrator uploads the carrier's policy wordings as PDFs on the **Policies**
screen. Each one is read, cut into page-aware passages, embedded and written to its
own Qdrant collection — separate from claim material, which has a different
retention and access story. When a notification arrives, four facet queries are
built from it — identity, site, peril, cover — and the wordings that answer them are
ranked and shown with the clauses that answered, on their pages.

Two matchers now run against a notice and they answer different questions.
`policy_identification` matches the **book**: structured rows, compared field by
field, and confirming one of its candidates binds a contract. The library matches
the **wordings**, and its answer is deliberately **advisory** — it writes nothing.
A wording is a document somebody uploaded, and letting a retrieval score bind a
contract would mean a badly-read declarations page could attach a claim to the
wrong policy with no book row consulted. Where the two agree an officer has
corroboration from two independent methods; where they disagree, that is the most
useful thing on the screen.

```bash
# Load the twelve synthetic wordings and check they retrieve sensibly.
curl -H "Authorization: Bearer $TOKEN" -F file=@policy/POL-CP-4471-88210_*.pdf \
     http://localhost:8000/api/v1/policies/documents          # 202 accepted
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/api/v1/policies/documents          # ingestion status
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"insured_name":"Harborline Cold Storage","cause_of_loss":"Ammonia release"}' \
     http://localhost:8000/api/v1/policies/match
```

Nothing here is required. With no embedding provider the wordings are still chunked
and searched by Postgres full text, which on a policy number and an insured name is
a good backend rather than a degraded stub — the screen says which mode it is in
rather than leaving it to be inferred. The engine, the signals, the confidence
ladder and the measured accuracy against the synthetic corpus:
**[docs/policy-library.md](docs/policy-library.md)**.

---

## Claims Workbench

What a handler does to a claim after intake has finished with it. Eight writes and
one read, under `app/services/claims/` — deliberately a separate package from
`app/services/fnol/`, because that one works a *notification* and this one works
the *claim*.

```bash
TOKEN=...  # a claims-handler, claims-manager or claims-admin token

# The six operational tabs, in one read
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/sections

# A note, filed against the tab it was written on
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"body":"Chased the adjuster; visit now Thursday.","section":"inspection"}' \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/notes

# Raise the indemnity reserve by GBP 20,000. A SIGNED DELTA, not a new total.
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"movement_type":"indemnity","amount_minor":2000000,"rationale":"Revised after the survey."}' \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/reserve

# Approve the settlement. Refused without a reason, and refused if anything blocks it.
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"decision":"approve_settlement","reason":"Adjuster figure accepted."}' \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/decision

# Confirm a coverage section. A reason is needed only to depart from the proposal.
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"standpoint":"confirmed","claimed_minor":142600000}' \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/coverages/fire

# Add the loss adjuster — a role no broker's email ever names.
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"role":"loss_adjuster","name":"I. McGregor","organisation":"McGregor & Co"}' \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/parties

# Record an aggregate excess. Its remaining figure accounts for other claims.
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"deductible_type":"aggregate","amount_minor":2500000}' \
     http://localhost:8000/api/v1/claims/CLM-2026-000001/deductible
```

Two things about this module are worth knowing before reading the code.

**A reserve movement is a signed delta and the ledger is the definition of the
figure.** `claims.reserve_minor` is a cache of the sum of
`claim_reserve_movements`, kept in step by the one write that appends to it. A
movement of zero is refused by a database constraint, because a movement that
changes nothing explains nothing.

**Two of the six sections are not built, and the payload says so** rather than
filling the gaps with plausible defaults. Each section carries `available` and a
sentence saying why — the inspection and the recovery register have no model behind
them, and a screen that cannot tell "nothing commissioned" from "we do not record
visits" teaches a handler to distrust the sections that are real.

**Coverage sections are proposed, never decided.** The policy's named perils become
one row each at claim creation, and every one lands *in question* unless the policy
is bound and its checks passed. A failed peril check never excludes a section: a
rule flagging a problem is a reason to look, not a refusal to pay. What the rules
proposed is kept beside where the section stands, so an override reads as an
override — and a reason is required only when the two differ.

**Two rules about the excess are worth knowing before reading the code.** Only an
aggregate erodes across other claims on the policy; every other type starts whole
every time, and netting other losses off it would understate what the insured
carries. And a **franchise does not deduct** — it is a threshold, so a loss above it
pays in full. Treating it as an ordinary excess understates every settlement that
clears it.

The state machine, what blocks a settlement, the ledger arithmetic, the coverage
model and the honest scoring against the RFP:
**[docs/claims-workbench.md](docs/claims-workbench.md)**.
