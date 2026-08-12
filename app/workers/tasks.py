"""Celery tasks.

Celery workers are synchronous, but the application's data access is async.
`run_async` bridges the two with a per-process event loop, so tasks can reuse
the same repositories and services the API uses instead of duplicating them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.core.config import settings
from app.core.logging import get_logger
from app.db.pool import close_pool, init_pool
from app.db.session import dispose_engine, init_engine
from app.services.cache import close_redis, init_redis
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """One event loop per worker process, reused across tasks.

    Reusing the loop is what lets the asyncpg pool and Redis client survive
    between tasks rather than being rebuilt on every invocation.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop.run_until_complete(_startup())
    return _loop


async def _startup() -> None:
    await init_engine(settings)
    await init_pool(settings)
    await init_redis(settings)


async def _shutdown() -> None:
    await close_redis()
    await close_pool()
    await dispose_engine()


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine on the worker's event loop."""
    return _get_loop().run_until_complete(coro)


def shutdown_worker_resources() -> None:
    """Tear down the worker's async resources. Wired to worker shutdown."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.run_until_complete(_shutdown())
        _loop.close()
        _loop = None


@celery_app.task(name="app.workers.tasks.heartbeat")
def heartbeat() -> dict[str, str]:
    """Beat-scheduled no-op, so a broken schedule is visible in logs."""
    logger.info("celery_heartbeat")
    return {"status": "ok"}


@celery_app.task(
    name="app.workers.tasks.ping_dependencies",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def ping_dependencies(self: Any) -> dict[str, bool]:
    """Verify the worker can reach Postgres and Redis. Also a template for
    async-capable tasks: do the work in a coroutine, bridge with `run_async`."""

    async def _check() -> dict[str, bool]:
        from app.db import pool
        from app.services.cache import ping_redis

        return {
            "postgres": await pool.fetchval("SELECT 1") == 1,
            "redis": await ping_redis(),
        }

    try:
        return run_async(_check())
    except Exception as exc:
        logger.error("celery_ping_failed", error=str(exc))
        raise self.retry(exc=exc) from exc
