"""Claims handler reads and workload bookkeeping."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
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
