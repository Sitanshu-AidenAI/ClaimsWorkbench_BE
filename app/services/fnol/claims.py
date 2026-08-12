"""Turning a reviewed notice into a claim.

The one operation in this module that cannot be got wrong twice. Three guarantees,
in the order they are enforced:

1. **It is refused when it should be.** Blocking exceptions and missing critical
   fields are checked before anything is written, and the refusal names exactly
   what stands in the way.
2. **It happens at most once.** The notice row is locked for the transaction, and
   the first thing the locked path does is look for a claim that already exists.
   A retried request — a double click, a client retry, a proxy replay — returns
   the claim the first one made rather than making a second.
3. **It is all-or-nothing.** The claim, the link back to the notice, the triage,
   the assignment recommendation and the audit events are one transaction. The
   caller commits; this service never does.

Nothing about the notice is destroyed. The claim carries a copy of the loss facts
so its own record is stable, and the FNOL keeps its documents, its intelligence
and its trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.errors import ConflictError, ValidationError
from app.core.logging import get_logger
from app.domain.enums import (
    ActorType,
    AuditEventType,
    ClaimStatus,
    ExceptionStatus,
    FNOLStatus,
)
from app.domain.lifecycle import blocking_codes, can_transition
from app.domain.references import CLAIM_PREFIX
from app.models.claim import Claim
from app.models.fnol import FNOLCase
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.reference import ReferenceRepository
from app.services.fnol.audit import AuditService
from app.services.fnol.triage import AssignmentService, TriageService

logger = get_logger(__name__)


@dataclass(slots=True)
class Blocker:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(slots=True)
class ClaimCreationResult:
    claim: Claim
    #: True when this call created it; false when it returned an existing one.
    created: bool
    blockers: list[Blocker] = field(default_factory=list)


class ClaimCreationBlocked(ValidationError):
    """Refused because the notice is not ready. Carries the checklist."""

    code = "claim_creation_blocked"

    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__(
            "This notification cannot become a claim yet.",
            details={"blockers": [blocker.as_dict() for blocker in blockers]},
        )
        self.blockers = blockers


class ClaimCreationService:
    def __init__(
        self,
        *,
        fnol: FNOLRepository,
        claims: ClaimRepository,
        references: ReferenceRepository,
        triage: TriageService,
        assignment: AssignmentService,
        audit: AuditService,
    ) -> None:
        self._fnol = fnol
        self._claims = claims
        self._references = references
        self._triage = triage
        self._assignment = assignment
        self._audit = audit

    async def blockers(self, case: FNOLCase) -> list[Blocker]:
        """What stands between this notice and a claim, in the order to fix it."""
        found: list[Blocker] = []

        open_exceptions = await self._fnol.list_exceptions(case.id, only_open=True)
        by_code = {exception.code: exception for exception in open_exceptions}

        for code in blocking_codes(by_code):
            exception = by_code[code.value]
            found.append(Blocker(code=code.value, message=exception.title))

        if not case.policy_id:
            found.append(
                Blocker(
                    code="policy_required",
                    message="A policy must be selected before the claim is created.",
                )
            )
        if not case.date_of_loss:
            found.append(
                Blocker(code="date_of_loss_required", message="The date of loss is missing.")
            )
        if not case.loss_description:
            found.append(
                Blocker(
                    code="loss_description_required",
                    message="A description of the loss is missing.",
                )
            )
        if not (case.insured_name or await self._claimant_name(case)):
            found.append(
                Blocker(
                    code="claimant_required",
                    message="An insured or claimant name is missing.",
                )
            )

        return found

    async def _claimant_name(self, case: FNOLCase) -> str | None:
        """Who is claiming, read from the parties rather than stored twice.

        The claimant is a party, and modelling it as a column on the case as well
        would leave two places to correct it and one of them stale. The claim
        takes a copy at creation, which is where a stable snapshot is wanted.
        """
        parties = await self._fnol.list_parties(case.id)
        claimant = next((party for party in parties if party.role == "claimant"), None)
        if claimant is not None:
            return claimant.name
        insured = next((party for party in parties if party.role == "insured"), None)
        return insured.name if insured else None

    async def create(
        self, case_id: Any, *, actor: str, idempotency_key: str | None = None
    ) -> ClaimCreationResult:
        """Create the claim, or return the one this notice already became."""
        case = await self._fnol.get_for_update(case_id)
        if case is None:
            raise ConflictError("The notification no longer exists.")

        existing = await self._claims.get_by_fnol(case.id)
        if existing is not None:
            logger.info(
                "claim_creation_idempotent_hit",
                fnol=case.reference,
                claim=existing.reference,
            )
            return ClaimCreationResult(claim=existing, created=False)

        if case.status == FNOLStatus.CLAIM_CREATED:
            # Belt and braces: the status says a claim exists but no row does,
            # which is a data problem rather than a retry, and must not be papered
            # over by creating a second claim.
            raise ConflictError(
                "This notification is marked as converted but has no claim attached. "
                "Refer it to an administrator."
            )

        blockers = await self.blockers(case)
        if blockers:
            raise ClaimCreationBlocked(blockers)

        if not can_transition(FNOLStatus(case.status), FNOLStatus.CLAIM_CREATED):
            case.status = FNOLStatus.READY_FOR_CLAIM

        reference = await self._references.next_reference(CLAIM_PREFIX)
        claimant_name = await self._claimant_name(case)
        now = datetime.now(UTC)

        claim = Claim(
            reference=reference,
            fnol_case_id=case.id,
            status=ClaimStatus.FNOL,
            policy_id=case.policy_id,
            policy_number=case.policy_number,
            insured_name=case.insured_name,
            claimant_name=claimant_name or case.insured_name,
            line_of_business=case.line_of_business,
            claim_type=case.claim_type,
            loss_type=case.loss_type,
            loss_description=case.loss_description,
            loss_location=case.loss_location,
            loss_country=case.loss_country,
            date_of_loss=case.date_of_loss,
            reported_at=case.received_at or now,
            severity=case.severity,
            reserve_minor=case.estimated_loss_minor or 0,
            currency=case.currency or "GBP",
            cat_event_id=case.cat_event_id if case.cat_confirmed else None,
            created_by=actor,
        )
        self._claims.add(claim)
        # Flushed so the claim has an id for the triage, assignment and audit rows
        # written below — all inside the same transaction the caller commits.
        await self._claims.flush()

        case.claim_id = claim.id
        case.status = FNOLStatus.CLAIM_CREATED

        self._audit.claim(
            claim,
            event_type=AuditEventType.CLAIM_CREATED,
            summary=f"Claim {claim.reference} created from notification {case.reference}.",
            actor=actor,
            after={
                "reference": claim.reference,
                "policy_number": claim.policy_number,
                "reserve_minor": claim.reserve_minor,
                "severity": claim.severity,
            },
            context={"fnol_reference": case.reference, "idempotency_key": idempotency_key},
        )
        self._audit.fnol(
            case,
            event_type=AuditEventType.FNOL_STATUS_CHANGED,
            summary=f"Notification converted to claim {claim.reference}.",
            actor=actor,
            before={"status": FNOLStatus.READY_FOR_CLAIM.value},
            after={"status": FNOLStatus.CLAIM_CREATED.value, "claim": claim.reference},
        )

        triage_row = await self._triage.run(claim, case)
        self._audit.claim(
            claim,
            event_type=AuditEventType.TRIAGE_COMPLETED,
            summary=(
                f"Triaged to {triage_row.recommended_route} at "
                f"{triage_row.recommended_priority} priority."
            ),
            actor="Triage engine",
            actor_type=ActorType.AI,
            after={
                "route": triage_row.recommended_route,
                "priority": triage_row.recommended_priority,
                "categories": triage_row.categories,
            },
        )

        assignment = await self._assignment.recommend(claim, triage_row)
        self._audit.claim(
            claim,
            event_type=AuditEventType.HANDLER_ASSIGNED,
            summary=(
                f"Recommended {assignment.handler_name} ({assignment.team})."
                if assignment.handler_name
                else f"No handler qualified; queued to {assignment.queue}."
            ),
            actor="Assignment engine",
            actor_type=ActorType.AI,
            after={
                "handler": assignment.handler_name,
                "queue": assignment.queue,
                "status": assignment.status,
            },
        )

        logger.info(
            "claim_created",
            claim=claim.reference,
            fnol=case.reference,
            actor=actor,
            reserve_minor=claim.reserve_minor,
        )
        return ClaimCreationResult(claim=claim, created=True)


def open_exception_codes(exceptions: list[Any]) -> list[str]:
    return [exception.code for exception in exceptions if exception.status == ExceptionStatus.OPEN]
