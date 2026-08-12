"""SQLAlchemy async engine and session factory.

This is the ORM half of the data-access split: reference data, administrative
CRUD and reporting aggregates. Performance-critical transactional paths use the
asyncpg pool in `app.db.pool` instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(config: Settings | None = None) -> AsyncEngine:
    config = config or settings
    return create_async_engine(
        config.postgres.async_dsn,
        echo=config.postgres.sqlalchemy_echo,
        pool_size=config.postgres.sqlalchemy_pool_size,
        max_overflow=config.postgres.sqlalchemy_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


async def init_engine(config: Settings | None = None) -> AsyncEngine:
    """Create the process-wide engine and session factory."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(config)
        _session_factory = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info("sqlalchemy_engine_initialised")
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("sqlalchemy_engine_disposed")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("SQLAlchemy engine is not initialised; call init_engine() first.")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session scope for use outside the request lifecycle.

    Commits on clean exit, rolls back on exception. Request handlers should take
    the `SessionDep` dependency rather than calling this directly.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
