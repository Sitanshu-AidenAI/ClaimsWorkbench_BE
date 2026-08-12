"""Catastrophe event reads.

Narrowed by date window in SQL — the one dimension that genuinely eliminates
events — and scored on place and peril in Python, where the rule is readable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference_data import CatEvent


class CatEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, event_id: uuid.UUID) -> CatEvent | None:
        return await self._session.get(CatEvent, event_id)

    async def get_by_reference(self, reference: str) -> CatEvent | None:
        statement = select(CatEvent).where(CatEvent.reference == reference.strip().upper())
        return (await self._session.execute(statement)).scalars().first()

    async def find_in_window(
        self, loss_date: date, *, tolerance_days: int = 2, country: str | None = None
    ) -> Sequence[CatEvent]:
        """Events whose window contains the loss date, within a tolerance.

        The tolerance is there because a storm's official window and the date a
        roof actually came off are rarely the same day, and an event that missed
        by twelve hours is exactly the attribution an officer wants offered.
        """
        margin = timedelta(days=max(0, tolerance_days))
        statement = select(CatEvent).where(
            CatEvent.start_date <= loss_date + margin,
            CatEvent.end_date >= loss_date - margin,
        )
        if country:
            statement = statement.where(CatEvent.country.ilike(country.strip()))
        return (
            (await self._session.execute(statement.order_by(CatEvent.start_date))).scalars().all()
        )

    async def list_all(self, limit: int = 200) -> Sequence[CatEvent]:
        statement = select(CatEvent).order_by(CatEvent.start_date.desc()).limit(limit)
        return (await self._session.execute(statement)).scalars().all()
