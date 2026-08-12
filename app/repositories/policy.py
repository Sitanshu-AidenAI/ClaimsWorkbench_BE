"""Policy reads, shaped for matching.

The candidate query is deliberately generous — it narrows the table to a set the
scorer can afford to walk, and lets the scorer decide. A repository that tried to
be clever about which policy is "the" match would put the ranking rule in SQL,
where it cannot be tested, explained to an officer, or reused by the fraud
service.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.matching import normalise_reference
from app.models.reference_data import Policy

#: Ceiling on candidates handed to the scorer. A notice with nothing but a
#: country to go on should not drag the whole book into memory.
MAX_CANDIDATES = 200


class PolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, policy_id: uuid.UUID) -> Policy | None:
        return await self._session.get(Policy, policy_id)

    async def get_by_number(self, policy_number: str) -> Policy | None:
        normalised = normalise_reference(policy_number)
        if not normalised:
            return None
        statement = select(Policy).where(
            func.regexp_replace(func.lower(Policy.policy_number), r"[^a-z0-9]", "", "g")
            == normalised
        )
        return (await self._session.execute(statement)).scalars().first()

    async def find_candidates(
        self,
        *,
        policy_number: str | None = None,
        insured_name: str | None = None,
        organisation: str | None = None,
        broker: str | None = None,
        email: str | None = None,
        country: str | None = None,
        line_of_business: str | None = None,
        loss_date: date | None = None,
        limit: int = MAX_CANDIDATES,
    ) -> Sequence[Policy]:
        """Every policy worth scoring against this notice.

        Each supplied signal contributes an `OR` branch, so a notice with only a
        broker reference still gets candidates. When nothing at all is known the
        query returns policies in force on the loss date rather than the whole
        table — an empty candidate list and a 3,000-row one are equally useless to
        the officer.
        """
        clauses = []

        if policy_number:
            normalised = normalise_reference(policy_number)
            if normalised:
                clauses.append(
                    func.regexp_replace(
                        func.lower(Policy.policy_number), r"[^a-z0-9]", "", "g"
                    ).like(f"%{normalised}%")
                )
        if insured_name:
            clauses.append(Policy.insured_name.ilike(f"%{_first_word(insured_name)}%"))
        if organisation:
            clauses.append(Policy.insured_organisation.ilike(f"%{_first_word(organisation)}%"))
        if broker:
            clauses.append(Policy.broker_name.ilike(f"%{_first_word(broker)}%"))
            clauses.append(Policy.broker_reference.ilike(f"%{broker.strip()}%"))
        if email:
            clauses.append(Policy.insured_email.ilike(email.strip()))

        statement: Select[tuple[Policy]] = select(Policy)
        if clauses:
            statement = statement.where(or_(*clauses))
        else:
            if country:
                statement = statement.where(Policy.country.ilike(country.strip()))
            if line_of_business:
                statement = statement.where(Policy.line_of_business == line_of_business)
            if loss_date:
                statement = statement.where(
                    Policy.effective_date <= loss_date, Policy.expiry_date >= loss_date
                )

        return (
            (await self._session.execute(statement.order_by(Policy.policy_number).limit(limit)))
            .scalars()
            .all()
        )

    async def list_all(self, limit: int = 500) -> Sequence[Policy]:
        return (
            (
                await self._session.execute(
                    select(Policy).order_by(Policy.policy_number).limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def count(self) -> int:
        return int((await self._session.execute(select(func.count(Policy.id)))).scalar_one())


def _first_word(value: str) -> str:
    """The most distinctive token of a name, for the `ILIKE` narrowing step.

    "Northline Logistics Ltd" narrows on "Northline"; matching on the whole
    string would miss "Northline Logistics Limited", which is the case the scorer
    exists to resolve.
    """
    parts = [part for part in value.strip().split() if len(part) > 2]
    return parts[0] if parts else value.strip()
