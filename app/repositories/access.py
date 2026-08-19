"""Reading and writing the role-to-capability grants.

All the SQL for the access matrix. SQLAlchemy rather than asyncpg, per the split
`docs/architecture.md` describes: this is administrative configuration read a
handful of times and written rarely, not a hot transactional path, and the mapped
model is what makes `alembic check` meaningful.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access import RoleCapability


class AccessRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def matrix(self) -> dict[str, set[str]]:
        """Every grant, as role -> capabilities.

        The whole table in one query. It is a dozen roles by a dozen capabilities at
        the outside, so paging it or filtering by role would cost a round trip to
        save nothing — and the caller caches the result anyway.
        """
        rows = await self._session.execute(select(RoleCapability.role, RoleCapability.capability))
        grants: dict[str, set[str]] = {}
        for role, capability in rows:
            grants.setdefault(role, set()).add(capability)
        return grants

    async def is_empty(self) -> bool:
        """Whether anything has been granted at all — the seed's only question."""
        found = await self._session.execute(select(RoleCapability.role).limit(1))
        return found.first() is None

    async def replace_role(
        self, role: str, capabilities: set[str], *, granted_by: str | None
    ) -> None:
        """Set exactly these capabilities for one role.

        Delete-then-insert scoped to the one role, rather than a diff of individual
        cells. The board sends a role's whole column, so this makes the stored state
        equal to what the administrator was looking at — a diff would leave a grant
        standing if two administrators edited different cells of the same column at
        once, and "the last save wins for that column" is far easier to explain than
        a merge nobody asked for.
        """
        await self._session.execute(delete(RoleCapability).where(RoleCapability.role == role))
        if not capabilities:
            return

        await self._session.execute(
            insert(RoleCapability).values(
                [
                    {"role": role, "capability": capability, "granted_by": granted_by}
                    for capability in sorted(capabilities)
                ]
            )
        )

    async def count(self) -> int:
        """How many grants exist. What the boot log reports."""
        return await self._count()

    async def seed(self, matrix: dict[str, set[str]], *, granted_by: str | None = None) -> int:
        """Insert the documented defaults. Returns the total afterwards.

        `ON CONFLICT DO NOTHING` so the statement is safe to issue twice — but that is
        **not** on its own enough to protect a revoked grant, because a revoked row
        does not exist and so does not conflict. Whether to seed at all is
        `AccessService.seed`'s decision, and it only does so into an empty table. The
        reasoning, and the bug that produced it, are recorded there.
        """
        rows = [
            {"role": role, "capability": capability, "granted_by": granted_by}
            for role, capabilities in matrix.items()
            for capability in sorted(capabilities)
        ]
        if not rows:
            return 0

        # Returns the resulting total rather than how many rows were new. Neither
        # `rowcount` nor `RETURNING` reported dependably for a multi-row INSERT with
        # ON CONFLICT DO NOTHING through this driver — both came back empty for a
        # statement that demonstrably inserted forty-nine rows. "The matrix now holds
        # N grants" is both true and the thing an operator actually wants from the
        # boot log; "N were new" was neither.
        await self._session.execute(insert(RoleCapability).values(rows).on_conflict_do_nothing())
        await self._session.flush()
        return await self._count()

    async def _count(self) -> int:
        total = await self._session.execute(select(func.count()).select_from(RoleCapability))
        return int(total.scalar_one())
