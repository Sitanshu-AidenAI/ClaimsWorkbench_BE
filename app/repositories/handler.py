"""Claims handler reads and workload bookkeeping."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference_data import Handler


class HandlerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, handler_id: uuid.UUID) -> Handler | None:
        return await self._session.get(Handler, handler_id)

    async def get_by_subject(self, subject: str) -> Handler | None:
        statement = select(Handler).where(Handler.subject == subject)
        return (await self._session.execute(statement)).scalars().first()

    async def get_by_email(self, email: str) -> Handler | None:
        """One handler by address, case-insensitively.

        Used only to *adopt* a directory row when somebody signs in for the first
        time — a colleague whose seeded record predates their account. Matched with
        `lower()` on both sides because an address is not case-sensitive and a token
        may present it either way, and this comparison decides whether a real person
        claims their row or gets a duplicate.
        """
        statement = select(Handler).where(func.lower(Handler.email) == email.lower())
        return (await self._session.execute(statement)).scalars().first()

    def add(self, handler: Handler) -> Handler:
        self._session.add(handler)
        return handler

    async def flush(self) -> None:
        """Push pending inserts so a generated id is available.

        Not a commit: the caller still owns the transaction. Sessions here are built
        with `autoflush=False`, so a directory row that has just been added is
        invisible to the next query until this runs.
        """
        await self._session.flush()

    async def list_available(self) -> Sequence[Handler]:
        """Every active handler, for the assignment engine to score.

        Unfiltered on purpose. Skill, line, geography and severity are all part of
        one score, and pre-filtering on any of them in SQL would mean a claim with
        no perfectly-qualified handler returned nothing rather than the best
        available person plus the reason they are not ideal.
        """
        statement = select(Handler).where(Handler.active.is_(True)).order_by(Handler.full_name)
        return (await self._session.execute(statement)).scalars().all()

    async def increment_workload(self, handler_id: uuid.UUID, delta: int = 1) -> None:
        """Adjust the open-claim counter in the database, not in Python.

        A read-modify-write here would lose an increment whenever two claims are
        assigned to the same handler at once.
        """
        await self._session.execute(
            update(Handler)
            .where(Handler.id == handler_id)
            .values(open_claims=Handler.open_claims + delta)
        )
