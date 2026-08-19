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

There is now exactly one accepted credential. The unsigned `dev.<base64url(json)>`
format was removed along with the flag that honoured it: a bypass that exists in
code is a bypass, and the argument for it — that a laptop running only Postgres
and Redis could not otherwise exercise the API — is answered by a realm the
bootstrap script creates in one command.

---

## The browser holds a token it cannot keep

The SPA is deliberately **not** an OAuth client. It has no client id and never
receives an authorization code. Either the API exchanges an email and password for
tokens on its behalf (`POST /auth/login`, the primary path), or it runs the
authorization-code flow server-side (`GET /auth/sso` and `/auth/callback`). Both
leave the browser with the same thing: an opaque handle in an `HttpOnly` cookie plus
an access token held in a JavaScript variable.

Collecting the password ourselves is a deliberate trade rather than an oversight.
What it costs is MFA, required actions and federation on that path — the grant cannot
ask a second question — which is why the redirect path is kept alongside it rather
than deleted. What it buys is a sign-in screen that belongs to the product. The
password never touches a database of ours, and the endpoint that receives it is the
only one in the API that is rate-limited on two axes and refuses in one sentence.

The split is the point. Two credentials exist and they have different exposures:

| | Where it lives | What an XSS can do with it |
| --- | --- | --- |
| Access token | JS memory, ~5 min | Use it until the tab closes |
| Refresh token | Redis, server-side | Nothing — it never enters the browser |

So script injection buys an attacker the length of a session in an open tab rather
than a credential they can take away. That is a meaningfully smaller prize than a
token in `localStorage`, which is what this replaced.

**Why not a full BFF**, with the access token server-side too and every request
authenticated by cookie? Because it would put a cookie on every API call, and a
cookie on every call requires the SPA and the API to be same-site — which
`CWB_CORS_ORIGINS` exists precisely to avoid requiring. Under the model chosen,
ordinary requests are plain bearer with no cookie, so cross-origin deployment
still works, `app/api/deps/auth.py` needs no cookie branch, and CSRF shrinks from
the whole API surface to two non-navigable POSTs.

The cost is that the access token is reachable by script while the tab is open.
That is the trade, stated rather than hidden: a `SecurityHeadersMiddleware` CSP is
the control that reduces the chance of injection in the first place, and it sits
in the middleware stack beside the session it protects for that reason.

**Rotation with reuse detection** is what makes the cookie worth less than it
looks. Each refresh retires its handle; a retired handle presented again destroys
the whole grant family, because two parties holding one cookie has no innocent
explanation worth being wrong about. A retired handle is therefore *kept* in Redis
rather than deleted — deleting it would make a replay look like an ordinary
expiry, and the theft would pass unnoticed.

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

## Mailbox intake commits per message, not per request

Everywhere else in this codebase the commit sits at the route boundary, so a
multi-service operation is atomic. `app.services.mail.intake` is the one
deliberate exception: it commits once per collected message.

A poll is a batch of independent notifications, and the rules that follow from
that are incompatible with one transaction:

* One message failing must not discard the nineteen collected before it.
* A failure must be *recorded*, and a row cannot be written inside a
  transaction that has just been rolled back — so the failure path opens its
  own.
* The mailbox is only touched after the notice is durable, which means a commit
  has to happen between the two.

The service therefore takes the session itself rather than leaving commit to the
caller, and `run_mail_intake` opens a plain session rather than `session_scope`,
whose closing commit would blur exactly the boundary that makes a partial batch
safe.

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

`GET /api/v1/meta/config` gives the client its runtime configuration rather than
baking it into the bundle per environment — one build artefact can be promoted
through environments. It no longer needs to carry OIDC endpoints for the client to
use, because the client does not perform the handshake; they remain in the payload
as deployment facts a support screen can display.

The cookie does constrain deployment in one place: it is scoped to
`Path=/api/v1/auth`, so the SPA and the API must agree on that origin for the two
session endpoints. Same-origin behind a reverse proxy is the simple answer, and is
what the Vite dev proxy already produces locally. Genuinely cross-site hosting
works, but needs `CWB_AUTH_COOKIE_SAMESITE=none`.

---

## Local ports

Host port mappings are off the defaults (Postgres 5442, Redis 6389, Keycloak
8090, MinIO 9020/9021, WireMock 8091, Mailpit 1035/8035) so this stack coexists
with other projects on a developer machine. Each is overridable through the
`CWB_*_PORT` variables. Inside the Compose network, services still talk to each
other on standard ports by service name.
