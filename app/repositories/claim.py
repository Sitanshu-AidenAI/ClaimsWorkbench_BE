"""Claim reads and writes, including the queue the handlers work."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.enums import ClaimStatus
from app.models.claim import Claim, ClaimAssignment, ClaimTriage

#: How far back the duplicate scan compares against created claims.
DUPLICATE_LOOKBACK_DAYS = 365

_PRIORITY_ORDER = case(
    {"urgent": 0, "high": 1, "standard": 2, "routine": 3},
    value=Claim.priority,
    else_=4,
)


class ClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, claim: Claim) -> Claim:
        self._session.add(claim)
        return claim

    async def flush(self) -> None:
        """Push pending inserts so generated ids are available.

        Not a commit: the caller still owns the transaction. This exists because
        the triage and audit rows written straight after a claim need its id.
        """
        await self._session.flush()

    async def get(self, claim_id: uuid.UUID) -> Claim | None:
        return await self._session.get(Claim, claim_id)

    async def get_by_reference(self, reference: str) -> Claim | None:
        statement = (
            select(Claim)
            .where(Claim.reference == reference.strip().upper())
            .options(selectinload(Claim.triage), selectinload(Claim.assignment))
        )
        return (await self._session.execute(statement)).scalars().first()

    async def get_by_fnol(self, fnol_case_id: uuid.UUID) -> Claim | None:
        """The claim a notice already became, if it became one.

        Read inside the create-claim transaction after the case row is locked,
        which is what makes a retried create idempotent rather than a second
        claim.
        """
        statement = select(Claim).where(Claim.fnol_case_id == fnol_case_id)
        return (await self._session.execute(statement)).scalars().first()

    async def list_queue(
        self,
        *,
        statuses: Sequence[str] | None = None,
        handler_name: str | None = None,
        created_by: str | None = None,
        severities: Sequence[str] | None = None,
        fraud_only: bool = False,
        cat_only: bool = False,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[Sequence[Claim], int]:
        statement = self._filtered(
            select(Claim).options(selectinload(Claim.triage), selectinload(Claim.assignment)),
            statuses=statuses,
            handler_name=handler_name,
            created_by=created_by,
            severities=severities,
            fraud_only=fraud_only,
            cat_only=cat_only,
            search=search,
        )
        total = int(
            (
                await self._session.execute(
                    self._filtered(
                        select(func.count(Claim.id)),
                        statuses=statuses,
                        handler_name=handler_name,
                        created_by=created_by,
                        severities=severities,
                        fraud_only=fraud_only,
                        cat_only=cat_only,
                        search=search,
                    )
                )
            ).scalar_one()
        )

        rows = (
            (
                await self._session.execute(
                    statement.order_by(_PRIORITY_ORDER, Claim.reported_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    def _filtered[T](
        self,
        statement: Select[T],
        *,
        statuses: Sequence[str] | None,
        handler_name: str | None,
        created_by: str | None,
        severities: Sequence[str] | None,
        fraud_only: bool,
        cat_only: bool,
        search: str | None,
    ) -> Select[T]:
        if statuses:
            statement = statement.where(Claim.status.in_(list(statuses)))
        if handler_name:
            statement = statement.where(Claim.handler_name == handler_name)
        if created_by:
            statement = statement.where(Claim.created_by == created_by)
        if severities:
            statement = statement.where(Claim.severity.in_(list(severities)))
        if fraud_only:
            statement = statement.where(Claim.fraud_flag.is_(True))
        if cat_only:
            statement = statement.where(Claim.cat_event_id.is_not(None))
        if search:
            pattern = f"%{search.strip()}%"
            statement = statement.where(
                or_(
                    Claim.reference.ilike(pattern),
                    Claim.claimant_name.ilike(pattern),
                    Claim.insured_name.ilike(pattern),
                    Claim.policy_number.ilike(pattern),
                    Claim.loss_location.ilike(pattern),
                )
            )
        return statement

    async def status_counts(self) -> dict[str, int]:
        statement = select(Claim.status, func.count(Claim.id)).group_by(Claim.status)
        return dict((await self._session.execute(statement)).all())

    async def open_exposure_minor(self) -> int:
        statement = select(func.coalesce(func.sum(Claim.reserve_minor), 0)).where(
            Claim.status.not_in([ClaimStatus.APPROVED, ClaimStatus.REJECTED])
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def count_where(
        self, *, fraud_only: bool = False, over_authority: bool = False, cat_only: bool = False
    ) -> int:
        statement = select(func.count(Claim.id))
        if fraud_only:
            statement = statement.where(Claim.fraud_flag.is_(True))
        if over_authority:
            statement = statement.where(Claim.over_authority.is_(True))
        if cat_only:
            statement = statement.where(Claim.cat_event_id.is_not(None))
        return int((await self._session.execute(statement)).scalar_one())

    async def duplicate_scan_pool(
        self,
        *,
        policy_number: str | None,
        insured_name: str | None,
        policy_id: uuid.UUID | None,
        date_of_loss: datetime | None,
        external_reference: str | None,
    ) -> Sequence[Claim]:
        """Claims a notice might be repeating."""
        since = datetime.now(UTC) - timedelta(days=DUPLICATE_LOOKBACK_DAYS)
        clauses = []
        if policy_number:
            clauses.append(Claim.policy_number.ilike(f"%{policy_number.strip()}%"))
        if policy_id:
            clauses.append(Claim.policy_id == policy_id)
        if insured_name:
            clauses.append(Claim.insured_name.ilike(f"%{_lead(insured_name)}%"))
        if date_of_loss:
            clauses.append(
                Claim.date_of_loss.between(
                    date_of_loss - timedelta(days=3), date_of_loss + timedelta(days=3)
                )
            )
        if external_reference:
            clauses.append(Claim.reference == external_reference.strip().upper())

        if not clauses:
            return []

        statement = (
            select(Claim)
            .where(Claim.reported_at >= since, or_(*clauses))
            .order_by(Claim.reported_at.desc())
            .limit(50)
        )
        return (await self._session.execute(statement)).scalars().all()

    # -- Triage and assignment ----------------------------------------------

    def add_triage(self, triage: ClaimTriage) -> ClaimTriage:
        self._session.add(triage)
        return triage

    async def get_triage(self, claim_id: uuid.UUID) -> ClaimTriage | None:
        statement = select(ClaimTriage).where(ClaimTriage.claim_id == claim_id)
        return (await self._session.execute(statement)).scalars().first()

    def add_assignment(self, assignment: ClaimAssignment) -> ClaimAssignment:
        self._session.add(assignment)
        return assignment

    async def get_assignment(self, claim_id: uuid.UUID) -> ClaimAssignment | None:
        statement = select(ClaimAssignment).where(ClaimAssignment.claim_id == claim_id)
        return (await self._session.execute(statement)).scalars().first()


def _lead(value: str) -> str:
    parts = [part for part in value.strip().split() if len(part) > 2]
    return parts[0] if parts else value.strip()
