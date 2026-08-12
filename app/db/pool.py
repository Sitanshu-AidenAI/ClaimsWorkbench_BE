"""asyncpg connection pool.

The raw-SQL half of the data-access split: performance-critical transactional
data, documents and audit, where hand-tuned queries (lateral aggregates,
`unnest` bulk inserts, conditional upserts) would fight an ORM.

JSON/JSONB columns are decoded to Python objects at the codec level so handlers
receive dicts rather than strings.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(connection: asyncpg.Connection) -> None:
    """Per-connection setup: JSON codecs."""
    for type_name in ("json", "jsonb"):
        await connection.set_type_codec(
            type_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def init_pool(config: Settings | None = None) -> asyncpg.Pool:
    """Create the process-wide asyncpg pool."""
    global _pool
    config = config or settings
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=config.postgres.asyncpg_dsn,
            min_size=config.postgres.pool_min_size,
            max_size=config.postgres.pool_max_size,
            command_timeout=config.postgres.pool_command_timeout,
            init=_init_connection,
        )
        logger.info(
            "asyncpg_pool_initialised",
            min_size=config.postgres.pool_min_size,
            max_size=config.postgres.pool_max_size,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("asyncpg_pool_closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("asyncpg pool is not initialised; call init_pool() first.")
    return _pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the pool."""
    async with get_pool().acquire() as connection:
        yield connection


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection and open a transaction on it."""
    async with get_pool().acquire() as connection, connection.transaction():
        yield connection


# --- Convenience helpers for the common single-statement cases -------------


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as connection:
        return await connection.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as connection:
        return await connection.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with acquire() as connection:
        return await connection.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as connection:
        return await connection.execute(query, *args)
