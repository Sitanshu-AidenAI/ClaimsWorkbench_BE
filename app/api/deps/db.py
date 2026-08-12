"""Database dependencies.

Which dependency a handler takes is what enforces the data-access split:

- `SessionDep`    — SQLAlchemy `AsyncSession` for reference/admin CRUD and
                    reporting aggregates.
- `ConnectionDep` — asyncpg connection for hand-written SQL on
                    performance-critical transactional paths.
- `TransactionDep` — as above, wrapped in a transaction for the request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.pool import acquire, transaction
from app.db.session import get_session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """A session per request. The handler owns commit/rollback semantics."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_connection() -> AsyncIterator[asyncpg.Connection]:
    async with acquire() as connection:
        yield connection


async def get_transaction() -> AsyncIterator[asyncpg.Connection]:
    async with transaction() as connection:
        yield connection


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ConnectionDep = Annotated[asyncpg.Connection, Depends(get_connection)]
TransactionDep = Annotated[asyncpg.Connection, Depends(get_transaction)]
