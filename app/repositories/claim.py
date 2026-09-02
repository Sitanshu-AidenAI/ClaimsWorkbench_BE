"""Claim reads and writes, including the queue the handlers work."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.enums import CLOSED_RECOVERY_STATUSES, ClaimStatus, DamageSeverity
from app.models.claim import (
    Claim,
    ClaimAssignment,
    ClaimCoverage,
    ClaimCoverageParty,
    ClaimDeductible,
    ClaimFraudDisposition,
    ClaimInspection,
    ClaimInspectionAction,
    ClaimInspectionObservation,
    ClaimNote,
    ClaimParty,
    ClaimRecovery,
    ClaimRecoveryEvent,
    ClaimRecoveryTask,
    ClaimReserveMovement,
    ClaimSiuCase,
    ClaimTriage,
)
from app.models.reference_data import Handler

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
        handler_subject: str | None = None,
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
            handler_subject=handler_subject,
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
                        handler_subject=handler_subject,
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
        handler_subject: str | None,
        created_by: str | None,
        severities: Sequence[str] | None,
        fraud_only: bool,
        cat_only: bool,
        search: str | None,
    ) -> Select[T]:
        if statuses:
            statement = statement.where(Claim.status.in_(list(statuses)))
        if handler_subject:
            #: **"Assigned to me" resolves by account, not by display name.**
            #:
            #: It used to compare `Claim.handler_name` against whatever the token
            #: called the reader, which meant a handler saw their own claims only
            #: while two unrelated strings happened to agree — the directory's
            #: `full_name` and the identity provider's. They did not agree, so an
            #: assigned claim left the manager's queue and appeared on nobody's.
            #:
            #: Through the assignment to the directory row, because `subject` is
            #: the one identifier that survives a rename and a change of address.
            #: The name predicate below is kept for the callers that genuinely mean
            #: a name — a search, a report — and is no longer how a person finds
            #: their own work.
            statement = statement.where(
                Claim.id.in_(
                    select(ClaimAssignment.claim_id)
                    .join(Handler, Handler.id == ClaimAssignment.handler_id)
                    .where(Handler.subject == handler_subject)
                )
            )
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

    # -- Notes ----------------------------------------------------------------

    async def list_notes(
        self, claim_id: uuid.UUID, *, section: str | None = None
    ) -> Sequence[ClaimNote]:
        """The claim's notes, newest first.

        Newest first because that is the order the tabs and the activity log both
        read them in. A tab that wants oldest-first reverses a list of ten rather
        than issuing a second query.
        """
        statement = select(ClaimNote).where(ClaimNote.claim_id == claim_id)
        if section is not None:
            statement = statement.where(ClaimNote.section == section)
        return (
            (await self._session.execute(statement.order_by(ClaimNote.created_at.desc())))
            .scalars()
            .all()
        )

    def add_note(self, claim_id: uuid.UUID, *, author: str, body: str, section: str) -> ClaimNote:
        note = ClaimNote(claim_id=claim_id, author=author, body=body, section=section)
        self._session.add(note)
        return note

    # -- The ledger -----------------------------------------------------------

    async def list_movements(self, claim_id: uuid.UUID) -> Sequence[ClaimReserveMovement]:
        """Every movement on the claim, newest first.

        Unpaginated on purpose. `previous_held` in the domain layer needs the whole
        series to answer "what was held before the last movement", and a claim with
        enough reserve movements to need a page of them is a claim somebody wants to
        read end to end anyway.
        """
        statement = (
            select(ClaimReserveMovement)
            .where(ClaimReserveMovement.claim_id == claim_id)
            .order_by(
                ClaimReserveMovement.occurred_at.desc(),
                ClaimReserveMovement.created_at.desc(),
            )
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_movement(self, movement: ClaimReserveMovement) -> ClaimReserveMovement:
        self._session.add(movement)
        return movement

    # -- Coverage sections ----------------------------------------------------

    async def list_coverages(self, claim_id: uuid.UUID) -> Sequence[ClaimCoverage]:
        """The claim's sections, in a stable order.

        Ordered by `section_key` rather than by creation or by standpoint. The tab
        is read as a list of the contract's sections, and ordering by standpoint
        would make a row jump position the moment somebody took a decision on it.
        """
        statement = (
            select(ClaimCoverage)
            .where(ClaimCoverage.claim_id == claim_id)
            .order_by(ClaimCoverage.section_key)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_coverage(self, claim_id: uuid.UUID, section_key: str) -> ClaimCoverage | None:
        """One section, addressed the way the API addresses it.

        By `(claim_id, section_key)` rather than by id, because the section key is
        what a caller has: it is derived from the peril and is stable across
        re-proposals, which an id is not.
        """
        statement = select(ClaimCoverage).where(
            ClaimCoverage.claim_id == claim_id,
            ClaimCoverage.section_key == section_key.strip().lower(),
        )
        return (await self._session.execute(statement)).scalars().first()

    def add_coverage(self, coverage: ClaimCoverage) -> ClaimCoverage:
        self._session.add(coverage)
        return coverage

    # -- Parties --------------------------------------------------------------

    async def list_parties(self, claim_id: uuid.UUID) -> Sequence[ClaimParty]:
        """The claim's parties, primaries first then alphabetically.

        Primary first because the insured and the claimant are what a handler looks
        for, and alphabetical after that because any other order — role, creation —
        makes a party hard to find in a list of fifteen on a liability claim.
        """
        statement = (
            select(ClaimParty)
            .where(ClaimParty.claim_id == claim_id)
            .order_by(ClaimParty.is_primary.desc(), ClaimParty.name)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_party(self, claim_id: uuid.UUID, party_id: uuid.UUID) -> ClaimParty | None:
        """One party, scoped to the claim.

        The claim id is in the predicate rather than trusted from the path: a party
        id from another claim must miss rather than resolve.
        """
        statement = select(ClaimParty).where(
            ClaimParty.claim_id == claim_id, ClaimParty.id == party_id
        )
        return (await self._session.execute(statement)).scalars().first()

    def add_party(self, party: ClaimParty) -> ClaimParty:
        self._session.add(party)
        return party

    # -- Party-to-coverage links ----------------------------------------------

    async def list_coverage_links(self, claim_id: uuid.UUID) -> Sequence[ClaimCoverageParty]:
        """Every party-to-section link on the claim, in one read.

        Joined through the coverage rather than queried per section, because the
        assessment tab draws all of them at once and one query per section would be
        a request per row of the contract.
        """
        statement = (
            select(ClaimCoverageParty)
            .join(ClaimCoverage, ClaimCoverage.id == ClaimCoverageParty.coverage_id)
            .where(ClaimCoverage.claim_id == claim_id)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_coverage_link(
        self, coverage_id: uuid.UUID, party_id: uuid.UUID
    ) -> ClaimCoverageParty | None:
        statement = select(ClaimCoverageParty).where(
            ClaimCoverageParty.coverage_id == coverage_id,
            ClaimCoverageParty.party_id == party_id,
        )
        return (await self._session.execute(statement)).scalars().first()

    def add_coverage_link(self, link: ClaimCoverageParty) -> ClaimCoverageParty:
        self._session.add(link)
        return link

    async def delete_coverage_link(self, link: ClaimCoverageParty) -> None:
        await self._session.delete(link)

    # -- Deductibles ----------------------------------------------------------

    async def list_deductibles(self, claim_id: uuid.UUID) -> Sequence[ClaimDeductible]:
        """The claim's excesses, the contract-level one first.

        Nulls first is deliberate rather than incidental: a null `coverage_id` means
        the excess applies to the whole claim, and that is the one a handler reads
        before any per-section one.
        """
        statement = (
            select(ClaimDeductible)
            .where(ClaimDeductible.claim_id == claim_id)
            .order_by(ClaimDeductible.coverage_id.nulls_first(), ClaimDeductible.created_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_deductible(self, deductible: ClaimDeductible) -> ClaimDeductible:
        self._session.add(deductible)
        return deductible

    async def applied_deductibles_on_other_claims(
        self, *, policy_id: uuid.UUID | None, exclude_claim_id: uuid.UUID
    ) -> list[int]:
        """What other claims on this policy have taken off the same aggregate.

        The input to `app.domain.coverage.erosion`, and the reason it is a query
        rather than a column: an aggregate excess is a property of the *contract*,
        so what is left of it cannot be known from one claim's rows. Returns an
        empty list for an unknown policy — the same answer, and the same reason, as
        `claims_on_policy`.
        """
        if policy_id is None:
            return []
        statement = (
            select(ClaimDeductible.applied_minor)
            .join(Claim, Claim.id == ClaimDeductible.claim_id)
            .where(
                Claim.policy_id == policy_id,
                Claim.id != exclude_claim_id,
                ClaimDeductible.applied_minor > 0,
            )
        )
        return [int(row) for row in (await self._session.execute(statement)).scalars().all()]

    # -- The inspection ------------------------------------------------------

    async def get_inspection(self, claim_id: uuid.UUID) -> ClaimInspection | None:
        """The claim's visit, if one was ever commissioned."""
        statement = select(ClaimInspection).where(ClaimInspection.claim_id == claim_id)
        return (await self._session.execute(statement)).scalars().first()

    # -- Recoveries ----------------------------------------------------------

    async def list_recoveries(self, claim_id: uuid.UUID) -> Sequence[ClaimRecovery]:
        """The register, open work first and then by what is worth most.

        Not by when it was opened: a handler arrives at this tab to decide what to
        chase, and the largest live expectation is the answer to that far more often
        than the oldest one is. Closed recoveries sink to the bottom because they
        need nothing.
        """
        statement = (
            select(ClaimRecovery)
            .where(ClaimRecovery.claim_id == claim_id)
            .order_by(
                ClaimRecovery.status.in_(sorted(CLOSED_RECOVERY_STATUSES)),
                ClaimRecovery.expected_minor.desc(),
                ClaimRecovery.opened_at,
            )
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_recovery(
        self, claim_id: uuid.UUID, recovery_id: uuid.UUID
    ) -> ClaimRecovery | None:
        """One recovery, scoped to its claim.

        The claim id is in the predicate rather than trusted from the path, so an id
        from another claim misses rather than resolving across two files — the rule
        every scoped read here follows.
        """
        statement = select(ClaimRecovery).where(
            ClaimRecovery.claim_id == claim_id, ClaimRecovery.id == recovery_id
        )
        return (await self._session.execute(statement)).scalars().first()

    def add_recovery(self, recovery: ClaimRecovery) -> ClaimRecovery:
        self._session.add(recovery)
        return recovery

    async def list_recovery_events(self, claim_id: uuid.UUID) -> Sequence[ClaimRecoveryEvent]:
        """Every recovery's history on this claim, newest last.

        Joined through the recoveries rather than fetched per recovery: the tab draws
        one chronology across the whole register, and a query per row would be an
        N+1 on the section assembly.
        """
        statement = (
            select(ClaimRecoveryEvent)
            .join(ClaimRecovery, ClaimRecovery.id == ClaimRecoveryEvent.recovery_id)
            .where(ClaimRecovery.claim_id == claim_id)
            .order_by(ClaimRecoveryEvent.occurred_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_recovery_event(self, event: ClaimRecoveryEvent) -> ClaimRecoveryEvent:
        self._session.add(event)
        return event

    async def list_recovery_tasks(self, claim_id: uuid.UUID) -> Sequence[ClaimRecoveryTask]:
        """Outstanding first, then by when it is due, nulls last.

        The same order the inspection's actions use, and for the same reason: the
        list is a to-do rather than a history, and a dated task outranks an undated
        one instead of being buried under it.
        """
        statement = (
            select(ClaimRecoveryTask)
            .where(ClaimRecoveryTask.claim_id == claim_id)
            .order_by(
                ClaimRecoveryTask.done,
                ClaimRecoveryTask.due_at.nulls_last(),
                ClaimRecoveryTask.created_at,
            )
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_recovery_task(self, task: ClaimRecoveryTask) -> ClaimRecoveryTask:
        self._session.add(task)
        return task

    async def get_recovery_task(
        self, claim_id: uuid.UUID, task_id: uuid.UUID
    ) -> ClaimRecoveryTask | None:
        statement = select(ClaimRecoveryTask).where(
            ClaimRecoveryTask.claim_id == claim_id, ClaimRecoveryTask.id == task_id
        )
        return (await self._session.execute(statement)).scalars().first()

    # -- The SIU case --------------------------------------------------------

    async def get_siu_case(self, claim_id: uuid.UUID) -> ClaimSiuCase | None:
        """The investigation, if one was ever opened."""
        statement = select(ClaimSiuCase).where(ClaimSiuCase.claim_id == claim_id)
        return (await self._session.execute(statement)).scalars().first()

    def add_siu_case(self, case: ClaimSiuCase) -> ClaimSiuCase:
        self._session.add(case)
        return case

    async def list_fraud_dispositions(self, claim_id: uuid.UUID) -> Sequence[ClaimFraudDisposition]:
        statement = (
            select(ClaimFraudDisposition)
            .where(ClaimFraudDisposition.claim_id == claim_id)
            .order_by(ClaimFraudDisposition.reviewed_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_fraud_disposition(
        self, claim_id: uuid.UUID, code: str
    ) -> ClaimFraudDisposition | None:
        """One verdict, by the indicator's code.

        Read before every write, because a changed mind is an update to the one row
        the unique constraint allows rather than a second verdict on the same
        indicator.
        """
        statement = select(ClaimFraudDisposition).where(
            ClaimFraudDisposition.claim_id == claim_id, ClaimFraudDisposition.code == code
        )
        return (await self._session.execute(statement)).scalars().first()

    def add_fraud_disposition(self, disposition: ClaimFraudDisposition) -> ClaimFraudDisposition:
        self._session.add(disposition)
        return disposition

    # -- The manager's queue -------------------------------------------------

    async def escalated_claims(self) -> Sequence[tuple[Claim, ClaimAssignment | None]]:
        """Every escalated claim, with whoever is on the file.

        The approval queue's one read. `outerjoin` because an unassigned escalation
        is a real and important row — it is one of the blocking conditions a manager
        has to clear — and an inner join would hide exactly the claims that need them
        most.

        Ordered by how long they have waited, which the service turns into days.
        """
        statement = (
            select(Claim, ClaimAssignment)
            .outerjoin(ClaimAssignment, ClaimAssignment.claim_id == Claim.id)
            .where(Claim.status == ClaimStatus.ESCALATED)
            .order_by(_PRIORITY_ORDER, Claim.updated_at)
        )
        return [(row[0], row[1]) for row in (await self._session.execute(statement)).all()]

    async def approved_since(self, moment: datetime) -> Sequence[Claim]:
        """Claims approved since `moment`. The queue's approved-month-to-date tile.

        `closed_at` rather than `updated_at`: a claim whose note was edited after
        approval has not been approved twice.
        """
        statement = select(Claim).where(
            Claim.status == ClaimStatus.APPROVED, Claim.closed_at >= moment
        )
        return (await self._session.execute(statement)).scalars().all()

    async def inspection_queue(
        self,
        *,
        adjuster_subject: str | None = None,
        adjuster_email: str | None = None,
    ) -> Sequence[tuple[ClaimInspection, Claim, int, str | None, int]]:
        """Every commissioned inspection, with its claim and its three totals.

        **One query, and that is the requirement rather than a preference.** The
        adjuster's board draws six chip counts, four metric tiles and a row per
        inspection, and every one of those needs the quantified total and the open
        action count. Fetching them per row would be three round trips times the
        length of the book — and the counts are computed over the *whole* set, so a
        page would not even bound it.

        The two aggregates are scalar subqueries rather than joins. A join to
        `claim_inspection_observations` would multiply the inspection row by its
        observations and then need a `GROUP BY` over every selected column of two
        tables, which is both slower and the kind of query that silently starts
        double-counting the moment somebody adds a column.

        `quantified_minor` sums **only priced observations and only affected
        elements** — the same rule `app.domain.inspection.quantified_minor` applies
        in Python, restated here because this one runs in the database. The two are
        kept honest by `test_the_sql_sum_agrees_with_the_domain_sum`; if they ever
        disagree the domain is right, because it is the one an auditor reads.

        Returns tuples rather than an ORM graph on purpose: the caller is building a
        flat payload, and `selectinload`ing observations it would only add up would
        pull every row of every schedule into memory to produce six integers.
        """
        priced = (
            select(func.coalesce(func.sum(ClaimInspectionObservation.quantified_minor), 0))
            .where(
                ClaimInspectionObservation.inspection_id == ClaimInspection.id,
                ClaimInspectionObservation.quantified_minor.is_not(None),
                #: Unaffected elements cost nothing by definition. See the domain.
                ClaimInspectionObservation.severity != DamageSeverity.UNAFFECTED,
            )
            .correlate(ClaimInspection)
            .scalar_subquery()
        )
        #: The currency the priced rows are in, so the caller can label the sum with
        #: the money it is actually in rather than with the claim's booking currency
        #: — the bug that reported a GBP schedule on a US claim as dollars.
        #:
        #: `min` rather than a distinct check because a mixed schedule cannot be
        #: written: `ClaimInspectionService.add_observation` refuses a currency the
        #: claim is not booked in. `app.domain.inspection.quantified_total` carries
        #: the second line of defence for rows that predate that rule.
        priced_currency = (
            select(func.min(ClaimInspectionObservation.currency))
            .where(
                ClaimInspectionObservation.inspection_id == ClaimInspection.id,
                ClaimInspectionObservation.quantified_minor.is_not(None),
                ClaimInspectionObservation.severity != DamageSeverity.UNAFFECTED,
            )
            .correlate(ClaimInspection)
            .scalar_subquery()
        )
        open_actions = (
            select(func.count(ClaimInspectionAction.id))
            .where(
                ClaimInspectionAction.inspection_id == ClaimInspection.id,
                ClaimInspectionAction.done.is_(False),
            )
            .correlate(ClaimInspection)
            .scalar_subquery()
        )

        statement = (
            select(ClaimInspection, Claim, priced, priced_currency, open_actions)
            .join(Claim, Claim.id == ClaimInspection.claim_id)
            #: Soonest due first, and undated last rather than first: an inspection
            #: with no due date is not urgent, it is unpromised, and floating it to
            #: the top of an adjuster's list would bury the ones that are late.
            .order_by(
                ClaimInspection.report_due_at.nulls_last(),
                ClaimInspection.commissioned_at.desc(),
            )
        )
        #: Narrowing to one adjuster's own work, and matched on identity rather than
        #: on `adjuster_name`. The name was free text a handler typed, so the filter
        #: it supported was a string comparison against a display name — which is
        #: why the board could only ever offer the whole desk and say so. Either
        #: column identifies the person: the subject once they have recorded
        #: something, the address before that. See `domain.inspection.owns_inspection`
        #: for why the address is only trusted while no subject has been claimed.
        clauses = []
        if adjuster_subject:
            clauses.append(ClaimInspection.adjuster_subject == adjuster_subject)
        if adjuster_email:
            clauses.append(
                and_(
                    ClaimInspection.adjuster_subject.is_(None),
                    func.lower(ClaimInspection.adjuster_email) == adjuster_email.strip().lower(),
                )
            )
        if clauses:
            statement = statement.where(or_(*clauses))

        rows = (await self._session.execute(statement)).all()
        return [(row[0], row[1], int(row[2]), row[3], int(row[4])) for row in rows]

    async def inspection_by_claim_reference(
        self, reference: str
    ) -> tuple[ClaimInspection, Claim] | None:
        """One inspection, addressed by the claim it was commissioned on.

        The adjuster's board puts the *claim* reference in its URL, not the
        inspection's id — which is right, because that is the number every party to
        the loss already has. There is one inspection per claim, so it resolves.
        """
        statement = (
            select(ClaimInspection, Claim)
            .join(Claim, Claim.id == ClaimInspection.claim_id)
            .where(Claim.reference == reference)
        )
        row = (await self._session.execute(statement)).first()
        return (row[0], row[1]) if row else None

    def add_inspection(self, inspection: ClaimInspection) -> ClaimInspection:
        self._session.add(inspection)
        return inspection

    async def list_observations(
        self, inspection_id: uuid.UUID
    ) -> Sequence[ClaimInspectionObservation]:
        """What the adjuster found, in the order they recorded it.

        By creation rather than by severity: a report reads as a walk round the
        site, and sorting by damage would scatter the roof, the racking and the
        stock across the list by how badly each came off.
        """
        statement = (
            select(ClaimInspectionObservation)
            .where(ClaimInspectionObservation.inspection_id == inspection_id)
            .order_by(ClaimInspectionObservation.created_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_observation(
        self, observation: ClaimInspectionObservation
    ) -> ClaimInspectionObservation:
        self._session.add(observation)
        return observation

    async def list_inspection_actions(
        self, inspection_id: uuid.UUID
    ) -> Sequence[ClaimInspectionAction]:
        """Outstanding work first, then by when it is due.

        Open before done, because the list is a to-do rather than a history — and
        nulls last within each group, so a dated action outranks an undated one
        instead of being buried under it.
        """
        statement = (
            select(ClaimInspectionAction)
            .where(ClaimInspectionAction.inspection_id == inspection_id)
            .order_by(
                ClaimInspectionAction.done,
                ClaimInspectionAction.due_at.nulls_last(),
                ClaimInspectionAction.created_at,
            )
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_inspection_action(self, action: ClaimInspectionAction) -> ClaimInspectionAction:
        self._session.add(action)
        return action

    async def get_inspection_action(
        self, inspection_id: uuid.UUID, action_id: uuid.UUID
    ) -> ClaimInspectionAction | None:
        """One action, scoped to its inspection.

        The inspection id is in the predicate rather than trusted from the path, so
        an action id from another claim misses rather than resolving across two
        files — the same rule `get_party` follows.
        """
        statement = select(ClaimInspectionAction).where(
            ClaimInspectionAction.inspection_id == inspection_id,
            ClaimInspectionAction.id == action_id,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def claims_on_policy(
        self, *, policy_id: uuid.UUID | None, exclude_claim_id: uuid.UUID, limit: int = 25
    ) -> Sequence[Claim]:
        """Other claims against the same policy, newest first.

        The loss history no single claim screen can show. Returns nothing rather
        than everything when the policy is unknown: an unmatched notice has no
        history, and falling back to "every claim on the book" would present
        unrelated losses as this claimant's record.
        """
        if policy_id is None:
            return []
        statement = (
            select(Claim)
            .where(Claim.policy_id == policy_id, Claim.id != exclude_claim_id)
            .order_by(Claim.reported_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()


def _lead(value: str) -> str:
    parts = [part for part in value.strip().split() if len(part) > 2]
    return parts[0] if parts else value.strip()
