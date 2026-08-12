"""Allocation of human references.

`FNOL-2026-000412` has to be unique and has to have no gaps a reader would
notice. Both are properties of the *allocation*, not of the format, so they are
enforced here with a row lock rather than in application code that hopes two
workers do not collide.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.references import format_reference
from app.models.reference_data import ReferenceSequence


class ReferenceRepository:
    """Hands out the next reference for a prefix and year."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_reference(self, prefix: str, *, year: int | None = None) -> str:
        """Allocate and return the next reference, locking the counter row.

        The upsert-then-lock shape matters: the first notice of a year has no row
        to lock, and `ON CONFLICT DO NOTHING` followed by `SELECT ... FOR UPDATE`
        is the only sequence that is correct whether or not the row already
        exists. The lock is held to the end of the caller's transaction, so a
        failed claim creation gives the number back.
        """
        year = year or datetime.now(UTC).year

        await self._session.execute(
            insert(ReferenceSequence)
            .values(prefix=prefix, year=year, last_value=0)
            .on_conflict_do_nothing(index_elements=["prefix", "year"])
        )

        row = (
            await self._session.execute(
                select(ReferenceSequence)
                .where(
                    ReferenceSequence.prefix == prefix,
                    ReferenceSequence.year == year,
                )
                .with_for_update()
            )
        ).scalar_one()

        row.last_value += 1
        await self._session.flush()
        return format_reference(prefix, year, row.last_value)
