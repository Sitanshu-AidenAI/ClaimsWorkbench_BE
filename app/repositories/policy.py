"""Policy reads, shaped for identification.

Two queries and a deliberate division of labour between them.

`find_identification_candidates` is the **retrieval** half of the engine: it
narrows the book to a set the scorer can afford to walk and then stops. Every
signal the notice carries contributes an `OR` branch, so a notice with nothing but
a broker's own reference still produces candidates, and a notice with nothing at
all falls back to the policies in force on the date of loss rather than to the
whole table — an empty candidate list and a three-thousand-row one are equally
useless to an officer.

`search` is the **manual** half: what an officer uses when the engine was wrong.
It answers on the same shape so that a policy found by hand goes through exactly
the same scoring, persistence and confirmation path as one the engine ranked.

Neither of them ranks. A repository that tried to be clever about which policy is
*the* match would put the ranking rule in SQL, where it cannot be unit-tested,
explained to an officer, or replayed by an auditor two years later.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.matching import normalise_reference
from app.models.reference_data import Policy, PolicyLocation

#: Ceiling on candidates handed to the scorer, when the caller does not say.
MAX_CANDIDATES = 250

#: A UK postcode's outward code — `LS11` of `LS11 8AX`. The outward half alone
#: narrows a book to a handful of locations, which makes it the right token to
#: retrieve on: the inward half is what the engine then scores precisely.
_OUTWARD_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*\d[A-Z]{2}\b", re.IGNORECASE)


class PolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, policy_id: uuid.UUID) -> Policy | None:
        statement = (
            select(Policy)
            .where(Policy.id == policy_id)
            .options(selectinload(Policy.locations_scheduled))
        )
        return (await self._session.execute(statement)).scalars().first()

    async def get_by_number(self, policy_number: str) -> Policy | None:
        normalised = normalise_reference(policy_number)
        if not normalised:
            return None
        statement = select(Policy).where(_normalised(Policy.policy_number) == normalised)
        return (await self._session.execute(statement)).scalars().first()

    async def find_identification_candidates(
        self,
        *,
        policy_number: str | None = None,
        broker_reference: str | None = None,
        contract_number: str | None = None,
        insured_name: str | None = None,
        organisation: str | None = None,
        broker_name: str | None = None,
        broker_domain: str | None = None,
        insured_domain: str | None = None,
        project_name: str | None = None,
        postcode: str | None = None,
        location: str | None = None,
        country: str | None = None,
        line_of_business: str | None = None,
        loss_date: date | None = None,
        limit: int = MAX_CANDIDATES,
    ) -> Sequence[Policy]:
        """Every policy worth scoring against this notice.

        Generous on purpose, and generous in a *particular* way: a reference the
        notice calls the policy number is searched against the policy number **and**
        against the broker's reference and the contract number, because brokers
        routinely put their own reference in the field a form labels "policy
        number" and construction desks put the contract there. Which of the three
        it actually is, is the engine's judgement to make and this query's job to
        make possible.
        """
        clauses: list[ColumnElement[bool]] = []

        # Any reference on the notice, against any reference on the policy. Three
        # by three rather than one by one, because the notice's labels are not
        # reliable and the policy's are.
        references = {
            normalise_reference(value)
            for value in (policy_number, broker_reference, contract_number)
            if value
        }
        for reference in {value for value in references if len(value) >= 4}:
            pattern = f"%{reference}%"
            clauses.extend(
                (
                    _normalised(Policy.policy_number).like(pattern),
                    _normalised(Policy.broker_reference).like(pattern),
                    _normalised(Policy.contract_number).like(pattern),
                    _normalised(Policy.project_reference).like(pattern),
                )
            )

        for name in (insured_name, organisation):
            token = _distinctive(name)
            if token:
                clauses.extend(
                    (
                        Policy.insured_name.ilike(f"%{token}%"),
                        Policy.insured_organisation.ilike(f"%{token}%"),
                        # Joint names: on a construction risk the party reporting is
                        # often a subcontractor, the principal or the main
                        # contractor rather than the first name on the declarations.
                        Policy.principal_name.ilike(f"%{token}%"),
                        Policy.contractor_name.ilike(f"%{token}%"),
                    )
                )

        broker_token = _distinctive(broker_name)
        if broker_token:
            clauses.append(Policy.broker_name.ilike(f"%{broker_token}%"))
        if broker_domain:
            clauses.append(Policy.broker_domain.ilike(broker_domain.strip()))
        if insured_domain:
            clauses.extend(
                (
                    Policy.insured_domain.ilike(insured_domain.strip()),
                    Policy.insured_email.ilike(f"%@{insured_domain.strip()}"),
                )
            )

        project_token = _distinctive(project_name)
        if project_token:
            clauses.extend(
                (
                    Policy.project_name.ilike(f"%{project_token}%"),
                    Policy.site_address.ilike(f"%{project_token}%"),
                )
            )

        # The postcode is retrieved on its outward code and against the *schedule*,
        # not against the primary location. A loss at warehouse seven of twelve is
        # exactly the case this exists for.
        outward = _outward(postcode) or _outward(location)
        if outward:
            clauses.append(
                Policy.id.in_(
                    select(PolicyLocation.policy_id).where(
                        PolicyLocation.postcode.ilike(f"{outward}%")
                    )
                )
            )
            clauses.extend(
                (
                    Policy.primary_location.ilike(f"%{outward}%"),
                    Policy.site_address.ilike(f"%{outward}%"),
                )
            )

        statement: Select[tuple[Policy]] = select(Policy).options(
            selectinload(Policy.locations_scheduled)
        )
        if clauses:
            statement = statement.where(or_(*clauses))
        else:
            # Nothing identifying was read. Narrowing to the policies that could
            # have answered this loss at all is more useful than either extreme,
            # and the engine's identification gate will still reject every one of
            # them unless something about the client agrees.
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

    async def search(self, term: str, *, limit: int = 25) -> Sequence[Policy]:
        """The policy book, searched by hand.

        One box rather than six, because an officer looking for a policy has one
        thing in front of them and does not know which field it is. Every axis the
        engine matches on is searched: references, both identities, the broker, the
        project, the contract and the schedule of locations.
        """
        needle = term.strip()
        if len(needle) < 2:
            return []

        pattern = f"%{needle}%"
        clauses: list[ColumnElement[bool]] = [
            Policy.policy_number.ilike(pattern),
            Policy.insured_name.ilike(pattern),
            Policy.insured_organisation.ilike(pattern),
            Policy.broker_name.ilike(pattern),
            Policy.broker_reference.ilike(pattern),
            Policy.project_name.ilike(pattern),
            Policy.project_reference.ilike(pattern),
            Policy.contract_number.ilike(pattern),
            Policy.principal_name.ilike(pattern),
            Policy.contractor_name.ilike(pattern),
            Policy.primary_location.ilike(pattern),
            Policy.site_address.ilike(pattern),
            Policy.policy_type.ilike(pattern),
            Policy.line_of_business.ilike(pattern),
            Policy.id.in_(
                select(PolicyLocation.policy_id).where(
                    or_(
                        PolicyLocation.address.ilike(pattern),
                        PolicyLocation.postcode.ilike(pattern),
                        PolicyLocation.location_ref.ilike(pattern),
                        PolicyLocation.description.ilike(pattern),
                    )
                )
            ),
        ]

        # A reference typed with different punctuation is the same reference. This
        # is the branch that makes `pol 2026 0041` find `POL-2026-0041`.
        reference = normalise_reference(needle)
        if len(reference) >= 4:
            clauses.extend(
                (
                    _normalised(Policy.policy_number).like(f"%{reference}%"),
                    _normalised(Policy.broker_reference).like(f"%{reference}%"),
                    _normalised(Policy.contract_number).like(f"%{reference}%"),
                )
            )

        statement = (
            select(Policy)
            .options(selectinload(Policy.locations_scheduled))
            .where(or_(*clauses))
            .order_by(Policy.status, Policy.policy_number)
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def list_all(self, limit: int = 500) -> Sequence[Policy]:
        return (
            (
                await self._session.execute(
                    select(Policy)
                    .options(selectinload(Policy.locations_scheduled))
                    .order_by(Policy.policy_number)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def count(self) -> int:
        return int((await self._session.execute(select(func.count(Policy.id)))).scalar_one())


def _normalised(column: Any) -> ColumnElement[str]:
    """A reference column with its punctuation dropped, for comparison in SQL.

    Mirrors `normalise_reference` exactly. At the size of a demo book this is free;
    on a real one it is the expression a functional index would be built over,
    which is why the same shape is used everywhere rather than open-coded per query.
    """
    return func.regexp_replace(func.lower(func.coalesce(column, "")), r"[^a-z0-9]", "", "g")


def _distinctive(value: str | None) -> str | None:
    """The most distinctive token of a name, for the `ILIKE` narrowing step.

    "Northline Logistics Ltd" narrows on "Northline"; matching on the whole string
    would miss "Northline Logistics Limited", which is the very case the engine
    exists to resolve. Company-form words are skipped — narrowing a book on
    "Limited" returns the book.
    """
    if not value:
        return None
    skip = {"limited", "ltd", "plc", "llp", "llc", "inc", "the", "and", "group", "holdings"}
    parts = [part for part in value.strip().split() if len(part) > 2 and part.lower() not in skip]
    return parts[0] if parts else (value.strip() or None)


def _outward(value: str | None) -> str | None:
    if not value:
        return None
    found = _OUTWARD_RE.search(value)
    if found:
        return found.group(1).upper()
    compact = value.strip().upper()
    if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", compact):
        return compact
    return None
