"""Redis client and cache helpers."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import ConnectionPool, Redis

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


async def init_redis(config: Settings | None = None) -> Redis:
    global _pool, _client
    config = config or settings
    if _client is None:
        _pool = ConnectionPool.from_url(
            config.redis.cache_dsn,
            decode_responses=True,
            health_check_interval=30,
        )
        _client = Redis(connection_pool=_pool)
        logger.info("redis_initialised", db=config.redis.db)
    return _client


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
        logger.info("redis_closed")


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis is not initialised; call init_redis() first.")
    return _client


async def ping_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:
        logger.warning("readiness_redis_failed", error=str(exc))
        return False


async def cache_get_json(key: str) -> Any | None:
    raw = await get_redis().get(key)
    return json.loads(raw) if raw else None


async def cache_set_json(key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
    await get_redis().set(key, json.dumps(value, default=str), ex=ttl_seconds)


async def cache_delete(*keys: str) -> int:
    if not keys:
        return 0
    return int(await get_redis().delete(*keys))
