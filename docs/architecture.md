# Architecture notes

Decisions that are not obvious from reading the code, and the reasoning behind
them. The stack itself is defined in
[foundational-tech-stack.md](../foundational-tech-stack.md).

---

## Two data-access paths, chosen by dependency

`asyncpg` and SQLAlchemy coexist rather than one replacing the other, and the
choice is expressed as a route dependency:

```python
async def timeline(conn: ConnectionDep): ...  # asyncpg, hand-written SQL
async def perils(session: SessionDep): ...  # SQLAlchemy AsyncSession
```

This is deliberate. Making it a dependency rather than a convention means the
choice is visible in the handler signature and reviewable in a diff — there is no
way to reach for the ORM on a hot transactional path without it being obvious.

- **asyncpg** — claims, documents, audit. Lateral aggregates, `unnest` bulk
  inserts and conditional upserts are all clearer as SQL than as ORM
  expressions, and there is no identity map or flush to reason about.
- **SQLAlchemy** — reference data, admin CRUD, reporting. The mapped models also
  serve as Alembic's `target_metadata`, which is what makes `alembic check`
  meaningful.

Both pools are created once per process in the lifespan and torn down on
shutdown. Under Gunicorn each worker has its own pair, so the Postgres
connection ceiling is `workers × (pool_max_size + sqlalchemy_pool_size)` — worth
recalculating before raising `CWB_WEB_CONCURRENCY`.

---

## Migrations are hand-written SQL

`--autogenerate` is a discovery tool, not the output. The generated diff is
rewritten as explicit `op.execute()` statements so the DDL that runs in
production is exactly what a reviewer read. It also means tables that only
asyncpg touches — which have no mapped model — are handled the same way as
everything else.

`alembic check` runs in CI to catch a mapped model that drifted from the
migration history.

---

## Token verification, never introspection

Keycloak issues tokens; the API verifies them locally against the realm JWKS.
Introspection would put a network call from Keycloak on every authenticated
request, making Keycloak a latency and availability dependency of every
endpoint. The JWKS is cached for `jwks_cache_ttl_seconds`, and an unrecognised
`kid` triggers exactly one refresh — which is what makes realm key rotation a
non-event rather than an outage.

`Principal` is a projection of the token, not a database lookup. Nothing on the
request path needs a user table read to authorise a call.

---

## One error shape

Every failure — application error, `HTTPException`, validation failure or
unhandled exception — leaves as:

```json
{ "error": { "code": "...", "message": "...", "details": {} }, "request_id": "..." }
```

The frontend gets one branch to write instead of one per endpoint, and the
`request_id` ties a user's screenshot to a log line. The corollary is that
handlers raise the types in `app/core/errors.py` rather than `HTTPException`,
because those carry a stable `code` the client can switch on — an HTTP status
alone is too coarse.

Unhandled exceptions are logged with a stack trace and returned as a generic
500. The exception message is never echoed to the client.

---

## Celery workers run async code

The application's data access is async; Celery workers are not. Rather than
maintaining synchronous duplicates of every repository, `app/workers/tasks.py`
keeps one event loop per worker process and bridges to it with `run_async`.

Reusing the loop is the point: the asyncpg pool and Redis client are created
once per process and survive between tasks, instead of being rebuilt on every
invocation.

---

## Metrics: no in-progress gauge

`should_instrument_requests_inprogress` is off. The instrumentator creates that
gauge inside its middleware's `__init__` and always registers it against the
global Prometheus registry, ignoring any registry passed in. Middleware classes
are re-instantiated whenever Starlette rebuilds the middleware stack, so the
second build raises a duplicate-collector error — which makes building two
applications in one process (as the test suite does) impossible.

Request counts, latency histograms and request/response sizes all honour the
supplied registry, and together they cover what the gauge would have told us.

---

## Frontend / backend boundary

The Vite dev server proxies `/api` to the backend, so the browser makes
same-origin requests in development and CORS stays out of the local loop.
`CWB_CORS_ORIGINS` still lists the frontend origin for the deployed case, where
the two are served from different hosts.

`GET /api/v1/meta/config` gives the client its OIDC endpoints at runtime rather
than baking them into the bundle per environment — one build artefact can be
promoted through environments.

---

## Local ports

Host port mappings are off the defaults (Postgres 5442, Redis 6389, Keycloak
8090, MinIO 9020/9021, WireMock 8091, Mailpit 1035/8035) so this stack coexists
with other projects on a developer machine. Each is overridable through the
`CWB_*_PORT` variables. Inside the Compose network, services still talk to each
other on standard ports by service name.
