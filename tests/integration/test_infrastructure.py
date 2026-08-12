"""Integration checks against real Postgres and Redis.

Run with `uv run pytest -m integration` once `docker compose up -d postgres redis`
has settled. Excluded from the default unit run.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.db import pool
from app.db.pool import close_pool, init_pool
from app.db.session import dispose_engine, init_engine, session_scope
from app.services.cache import cache_get_json, cache_set_json, close_redis, init_redis

pytestmark = pytest.mark.integration


@pytest.fixture
async def infrastructure() -> object:
    settings = get_settings()
    await init_engine(settings)
    await init_pool(settings)
    await init_redis(settings)
    yield settings
    await close_redis()
    await close_pool()
    await dispose_engine()


async def test_asyncpg_pool_executes_sql(infrastructure: object) -> None:
    assert await pool.fetchval("SELECT 1") == 1


async def test_json_columns_decode_to_python_objects(infrastructure: object) -> None:
    value = await pool.fetchval("SELECT '{\"a\": 1}'::jsonb")

    assert value == {"a": 1}


async def test_migrations_have_been_applied(infrastructure: object) -> None:
    version = await pool.fetchval("SELECT version_num FROM alembic_version")

    assert version is not None


async def test_updated_at_trigger_function_exists(infrastructure: object) -> None:
    exists = await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'set_updated_at')"
    )

    assert exists is True


async def test_sqlalchemy_session_executes_sql(infrastructure: object) -> None:
    from sqlalchemy import text

    async with session_scope() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def test_redis_round_trips_json(infrastructure: object) -> None:
    await cache_set_json("test:integration", {"hello": "world"}, ttl_seconds=30)

    assert await cache_get_json("test:integration") == {"hello": "world"}
