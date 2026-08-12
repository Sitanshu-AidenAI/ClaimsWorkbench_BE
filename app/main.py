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
from app.services.cache import close_redis, init_redis

logger = get_logger(__name__)


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
        logger.info("application_started")
        try:
            yield
        finally:
            await close_redis()
            await close_pool()
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
