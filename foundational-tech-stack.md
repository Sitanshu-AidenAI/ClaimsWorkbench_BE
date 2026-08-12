# Tech Stack

---

## Backend

| Layer               | Choice                                                                         | Version       |
| ------------------- | ------------------------------------------------------------------------------ | ------------- |
| Language            | Python                                                                         | 3.13          |
| Web framework       | FastAPI                                                                        | ≥0.110        |
| Validation / models | Pydantic v2                                                                    | ≥2.0          |
| Config              | pydantic-settings (`BaseSettings`, env prefix + `.env`)                        | ≥2.0          |
| ASGI server         | uvicorn workers under gunicorn (`UvicornWorker`)                               | ≥0.29 / ≥22.0 |
| Database            | PostgreSQL                                                                     | 16            |
| DB driver           | asyncpg — raw SQL on performance-critical transactional paths                          | ≥0.29         |
| ORM                 | SQLAlchemy 2.0 async — reference data, admin CRUD, Alembic autogenerate target | ≥2.0          |
| Migrations          | Alembic (+ psycopg2-binary for the sync path)                                  | ≥1.13         |
| Cache / broker      | Redis                                                                          | 7             |
| Background jobs     | Celery (+ Celery beat)                                                         | ≥5.3          |
| Object storage      | S3 via boto3 (MinIO locally)                                                   | ≥1.34         |
| Identity            | Keycloak (JWT verified in-app via `pyjwt[crypto]`)                             | 25            |
| HTTP client         | httpx                                                                          | ≥0.28         |
| Retries             | tenacity                                                                       | ≥9.0          |
| Logging             | structlog (JSON in prod)                                                       | ≥24.1         |
| Metrics             | prometheus-fastapi-instrumentator → `/metrics`                                 | ≥6.1          |
| PDF generation      | reportlab + markdown                                                           | ≥5.0 / ≥3.6   |
| HTML sanitising     | nh3                                                                            | ≥0.2          |
| Lint / format       | ruff (line-length 100; E,W,F,I,UP,B,SIM,C4,RUF)                                | ≥0.6          |
| Tests               | pytest, pytest-asyncio, httpx, respx                                           | —             |
| Dependencies        | **uv +** **`pyproject.toml`** **only** (single lockfile)                       | —             |

---

## Application entry point

The primary Python entry file must live at the **project root**, outside the `app/` package.

Recommended structure:

```text
project-root/
├── main.py
├── app/
│   ├── __init__.py
│   ├── api/
│   ├── core/
│   ├── models/
│   ├── services/
│   └── ...
├── pyproject.toml
└── ...
```

`main.py` is the application entry point and should expose the FastAPI application while also supporting direct execution with Uvicorn:

```python
import uvicorn

from app.main import app


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```

The `app/` directory contains the application package and implementation code. The root-level
`main.py` remains the executable entry point. Production process configuration may still run
Uvicorn workers under Gunicorn as defined in the backend stack above.

---

## Frontend

| Layer         | Choice                                                     | Version |
| ------------- | ---------------------------------------------------------- | ------- |
| Framework     | React                                                      | 19      |
| Language      | TypeScript          | 5.x     |
| Build         | Vite + `@vitejs/plugin-react`                              | 6       |
| Routing       | react-router-dom (lazy-loaded feature pages)               | 7       |
| State         | zustand                                                    | 5       |
| Styling       | Tailwind CSS + PostCSS + autoprefixer                      | 3.4     |
| Design tokens | local token module (single source for palette/type/easing) | —       |
| Motion        | framer-motion                                              | 12      |
| Icons         | lucide-react                                               | —       |
| Charts        | d3                                                         | 7       |
| Markdown      | react-markdown + remark-gfm + dompurify                    | —       |
| PDF viewing   | react-pdf                                                  | 9       |
| Unit tests    | vitest + jsdom                                             | 3 / 25  |
| E2E           | Playwright                                                 | 1.5x    |

---

## Infrastructure

| Service                    | Image                                                              |
| -------------------------- | ------------------------------------------------------------------ |
| API + worker + beat        | app image (`python:3.13-slim`)                                     |
| Database                   | `postgres:16-alpine`                                               |
| Cache / broker             | `redis:7-alpine`                                                   |
| Identity                   | `quay.io/keycloak/keycloak:25.0`                                   |
| Object storage             | `minio/minio` + `minio/mc` (init)                                  |
| External API stubs (dev/CI) | `wiremock/wiremock`                                                |
| Mail catcher (dev)         | `axllent/mailpit`                                                  |
| Orchestration              | Docker Compose locally; containers → ECS/EKS or equivalent in prod |
| CI                         | GitHub Actions — ruff, pytest, vitest, migration check             |

Pin third-party images by digest.

---

## External integrations

External service APIs · notification providers (WhatsApp / SMS / email) · e-sign provider where required.

---

## Deferred (add when needed, not at v1)

| Capability                                       | Stack                                        | When                             |
| ------------------------------------------------ | -------------------------------------------- | -------------------------------- |
| Semantic search / policy-wording RAG             | Qdrant + OpenAI embeddings                   | AI phase                         |
| LLM features (agent assist, doc Q&A)             | `openai` SDK + provider registry, MCP server | AI phase                         |
| Document OCR (RC / previous policy / claim docs) | PaddleOCR sidecar service                    | when auto-prefill is prioritised |

## Amendments to this document

**ORM added (was "raw SQL, no ORM").** SQLAlchemy 2.0 async sits alongside asyncpg
rather than replacing it. The split is deliberate and enforced by which route
dependency a handler takes:

- **asyncpg + hand-written SQL** — performance-critical transactional data, documents, and audit.
  Tuned queries (lateral aggregates, `unnest` bulk inserts, conditional upserts)
  where an ORM would be in the way.
- **SQLAlchemy** **`AsyncSession`** — reference and administrative CRUD, plus
  reporting aggregates. The mapped models are also Alembic's `target_metadata`,
  which is what makes `alembic check` and `--autogenerate` usable.

Migrations stay hand-written SQL. See the "Data access" section of `README.md`.

---

## Not included in the current stack

MongoDB · Memgraph · the PDF/DOCX/Excel extraction stack (pymupdf, pdfplumber, unstructured, surya, easyocr) · three.js / @react-three · @xyflow/react · docx-preview.
