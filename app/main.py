"""FastAPI application factory.

The executable entry point is the root-level `main.py`; this module owns
application assembly so it can be imported by tests, Celery and Gunicorn alike.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CollectorRegistry

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import AccessLogMiddleware, RequestContextMiddleware
from app.core.observability import setup_metrics
from app.db.pool import close_pool, init_pool
from app.db.session import dispose_engine, init_engine
from app.integrations.graph.client import close_mail_client
from app.services.cache import close_redis, init_redis
from app.services.intelligence.embedding import close_embedding_provider
from app.services.intelligence.vectors import close_vector_store

logger = get_logger(__name__)


async def _seed_extraction_schemas(config: Settings) -> None:
    """Create the bundled extraction datasets if they are missing.

    On boot rather than in a migration, because the seed is additive
    configuration rather than schema: a release that introduces a field should
    deliver it to a deployment that has already run its migrations, and a field
    an administrator has edited must survive that. Never fatal — a desk whose
    dataset could not be seeded still serves every other route, and the next boot
    tries again.
    """
    if not config.extraction.seed_on_startup:
        return

    from app.db.session import session_scope
    from app.repositories.extraction import ExtractionSchemaRepository
    from app.services.extraction.registry import seed_builtin_schemas

    try:
        async with session_scope() as session:
            await seed_builtin_schemas(ExtractionSchemaRepository(session))
    except Exception as exc:
        logger.warning("extraction_schema_seed_failed", error=type(exc).__name__, detail=str(exc))


def create_app(
    config: Settings | None = None,
    metrics_registry: CollectorRegistry | None = None,
) -> FastAPI:
    """Build the application.

    `metrics_registry` is only needed when more than one application is built in
    a single process (tests do this); it defaults to the global registry.
    """
    config = config or get_settings()
    configure_logging(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "application_starting",
            environment=config.environment,
            project=config.project_name,
        )
        await init_engine(config)
        await init_pool(config)
        await init_redis(config)
        await _seed_extraction_schemas(config)
        logger.info("application_started")
        try:
            yield
        finally:
            await close_redis()
            await close_pool()
            await close_mail_client()
            await close_embedding_provider()
            await close_vector_store()
            await dispose_engine()
            logger.info("application_stopped")

    app = FastAPI(
        title=config.project_name,
        version="0.1.0",
        debug=config.debug,
        docs_url=config.docs_url,
        redoc_url=None,
        openapi_url=config.openapi_url,
        lifespan=lifespan,
    )

    # Middleware is applied bottom-up, so the request-context layer is
    # registered last to make sure it wraps everything above it.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[config.observability.request_id_header, "X-Response-Time-ms"],
    )
    app.add_middleware(RequestContextMiddleware, config=config)

    register_exception_handlers(app)
    setup_metrics(app, config, registry=metrics_registry)

    app.include_router(api_router, prefix=config.api_v1_prefix)

    return app


app = create_app()
