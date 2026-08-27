"""The three things a handler can do to a claim: write, move money, decide.

One service rather than three, because all three share the same two obligations
and getting either wrong is the failure this module exists to prevent:

* **every one of them audits**, with an actor, and
* **every one of them leaves the claim's cached figures consistent** with the rows
  that explain them.

The second obligation is the load-bearing one. `claims.reserve_minor` is a cache
of the movement ledger's sum, and the only correct place to keep it in step is the
write that appends to the ledger — so the movement write does both, in one
transaction, and nothing else in the codebase is allowed to set the column.

Nothing here commits. The route owns the transaction, for the reason
`FNOLContext.commit` states: a decision that transitioned the claim but failed to
audit is worse than a decision that did not happen.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import ConflictError, ValidationError
from app.domain import claim_lifecycle
from app.domain.enums import (
    AuditEventType,
    ClaimDecisionAction,
    MovementType,
    NoteSection,
)
from app.models.claim import Claim, ClaimNote, ClaimReserveMovement
from app.repositories.claim import ClaimRepository
from app.repositories.handler import HandlerRepository
from app.services.fnol.audit import AuditService

#: Which audit event a movement announces itself as. A recovery and a reserve
#: raise are both rows in the same table and they are not the same event to a
#: reader: "money came back" and "we expect this to cost more" answer different
#: questions, and a trail that called both `reserve_moved` would make the first
#: unfindable.
_EVENT_FOR_MOVEMENT: dict[MovementType, AuditEventType] = {
    MovementType.INDEMNITY: AuditEventType.RESERVE_MOVED,
    MovementType.EXPENSE: AuditEventType.RESERVE_MOVED,
    MovementType.LEGAL: AuditEventType.RESERVE_MOVED,
    MovementType.RECOVERY: AuditEventType.RECOVERY_RECORDED,
}


class ClaimCaseworkService:
    def __init__(
        self,
        claims: ClaimRepository,
        handlers: HandlerRepository,
        audit: AuditService,
    ) -> None:
        self._claims = claims
        self._handlers = handlers
        self._audit = audit

    # -- Notes ---------------------------------------------------------------

    async def add_note(
        self, claim: Claim, *, body: str, section: NoteSection, actor: str
    ) -> ClaimNote:
        """Write a note on the claim, and audit that it was written.

        The note's *body* is deliberately not copied into the audit event. The note
        is itself a durable, attributed record, so duplicating its text would store
        the same sentence twice and leave two places to redact it from if it ever
        has to be. The event records that a note was added, and where.
        """
        note = self._claims.add_note(claim.id, author=actor, body=body, section=str(section))
        self._audit.claim(
            claim,
            event_type=AuditEventType.CLAIM_NOTE_ADDED,
            summary=f"{actor} added a note on {_section_phrase(section)}.",
            actor=actor,
            context={"section": str(section)},
        )
        return note

    # -- The ledger ----------------------------------------------------------

    async def post_movement(
        self,
        claim: Claim,
        *,
        movement_type: MovementType,
        amount_minor: int,
        rationale: str,
        actor: str,
        sub_movement_type: str | None = None,
        currency: str | None = None,
        accounting_amount_minor: int | None = None,
        accounting_currency: str | None = None,
        basis: str | None = None,
    ) -> ClaimReserveMovement:
        """Append a movement to the ledger and bring the claim's reserve with it.

        `amount_minor` is a signed delta — see `ClaimReserveMovement`. Two things
        are refused here rather than at the database:

        **A movement on a decided claim.** An approved or declined claim has had
        its figures reported; moving them afterwards is a restatement, which is a
        deliberate act with its own approval and not a side effect of somebody
        having the claim open in a tab.

        **A movement that would take the held reserve below zero.** The column has
        a non-negative constraint, so the alternative to checking here is a 500
        with a constraint name in it. A handler releasing more than is held has
        made an arithmetic mistake and is entitled to be told which figure they got
        wrong.

        Recoveries are appended and audited but do **not** move `reserve_minor`.
        The reserve is what the claim is expected to cost; money coming back is
        tracked against it, not netted out of it — the same reason
        `claim_lifecycle.incurred_minor` excludes them.
        """
        if claim_lifecycle.is_terminal(claim.status):
            raise ConflictError(
                f"Claim {claim.reference} is {claim.status.replace('_', ' ')}, so its "
                "figures are final. Reopening a decided claim is a separate act."
            )

        currency = (currency or claim.currency).upper()
        if movement_type is not MovementType.RECOVERY:
            held = (claim.reserve_minor or 0) + amount_minor
            if held < 0:
                raise ValidationError(
                    f"That would take the reserve to {held} minor units. "
                    f"{claim.reference} currently holds {claim.reserve_minor or 0}, so "
                    "at most that much can be released."
                )

        movement = self._claims.add_movement(
            ClaimReserveMovement(
                claim_id=claim.id,
                movement_type=str(movement_type),
                sub_movement_type=sub_movement_type,
                amount_minor=amount_minor,
                currency=currency,
                accounting_amount_minor=accounting_amount_minor,
                accounting_currency=(accounting_currency.upper() if accounting_currency else None),
                rationale=rationale,
                basis=basis,
                set_by=actor,
                occurred_at=datetime.now(UTC),
            )
        )

        before = claim.reserve_minor or 0
        if movement_type is not MovementType.RECOVERY:
            claim.reserve_minor = before + amount_minor
            await self._refresh_authority(claim)

        self._audit.claim(
            claim,
            event_type=_EVENT_FOR_MOVEMENT[movement_type],
            summary=(
                f"{actor} {_movement_phrase(movement_type, amount_minor)} "
                f"{_signed(amount_minor)} {currency}."
            ),
            actor=actor,
            before={"reserve_minor": before} if before != claim.reserve_minor else None,
            after=(
                {"reserve_minor": claim.reserve_minor} if before != claim.reserve_minor else None
            ),
            context={
                "movement_type": str(movement_type),
                "sub_movement_type": sub_movement_type,
                "amount_minor": amount_minor,
                "currency": currency,
                "rationale": rationale,
            },
        )
        return movement

    # -- Decisions -----------------------------------------------------------

    async def decide(
        self,
        claim: Claim,
        *,
        decision: ClaimDecisionAction,
        actor: str,
        reason: str | None = None,
    ) -> Claim:
        """Take a decision on the claim, subject to what blocks it.

        Three refusals, in this order, and the order is the point:

        1. **A decided claim cannot be decided again.** Checked first, because it is
           the one refusal that is true regardless of who is asking or what the
           figures say.
        2. **A committing decision is subject to `approval_blocks`.** The same
           function the workbench renders above the decision bar, so a handler is
           never refused for a reason the screen did not show them. A blocked
           attempt is *audited* rather than silently rejected: somebody trying to
           approve a claim £300,000 over their authority is a fact worth keeping.
        3. **The transition has to be legal.** Last, because by this point the
           first two have ruled out every case where an illegal transition would
           have been the less useful answer.
        """
        if claim_lifecycle.is_terminal(claim.status):
            raise ConflictError(
                f"Claim {claim.reference} is already {claim.status.replace('_', ' ')}. "
                "A decided claim has to be reopened before it can be decided again."
            )

        assignment = await self._claims.get_assignment(claim.id)
        blocks = claim_lifecycle.blocks_for_decision(
            decision,
            claim,
            assignment=assignment,
            authority_limit_minor=await self._authority_limit(assignment),
            authority_currency=await self._authority_currency(assignment),
            incurred_minor=await self._incurred(claim),
        )
        if blocks:
            self._audit.claim(
                claim,
                event_type=AuditEventType.CLAIM_DECISION_BLOCKED,
                summary=(
                    f"{actor} attempted to {claim_lifecycle.decision_phrase(decision)} "
                    f"and was blocked: {blocks[0].reason}."
                ),
                actor=actor,
                context={
                    "decision": str(decision),
                    "blocks": [str(block.code) for block in blocks],
                },
            )
            raise ValidationError(
                "This claim is not ready for that decision. " + _sentence_list(blocks) + ".",
                details={"blocks": blocks},
            )

        target = claim_lifecycle.status_for_decision(decision)
        if not claim_lifecycle.can_transition(claim.status, target):
            raise ConflictError(
                f"A claim that is {claim.status.replace('_', ' ')} cannot move to "
                f"{str(target).replace('_', ' ')}."
            )

        before = claim.status
        claim.status = str(target)
        if claim_lifecycle.is_terminal(claim.status):
            claim.closed_at = datetime.now(UTC)

        self._audit.claim(
            claim,
            event_type=AuditEventType.CLAIM_DECIDED,
            summary=claim_lifecycle.decision_summary(decision, actor=actor),
            actor=actor,
            before={"status": before},
            after={"status": claim.status},
            #: The verb is in the context rather than in the event type, so a reader
            #: filtering for decisions gets all five and can still tell a referral
            #: from a send-for-approval — the two that share a status.
            context=(
                {"decision": str(decision), "reason": reason}
                if reason
                else {"decision": str(decision)}
            ),
        )
        return claim

    # -- Shared reads --------------------------------------------------------

    async def _incurred(self, claim: Claim) -> int:
        """What the claim has cost, from the ledger rather than from the cache.

        The authority check reads the ledger directly. `reserve_minor` is a cache
        and this is the one place where trusting a cache would let a claim through
        an authority limit it is over.
        """
        movements = await self._claims.list_movements(claim.id)
        if not movements:
            return claim.reserve_minor or 0
        return claim_lifecycle.incurred_minor(movements)

    async def _authority_limit(self, assignment: object | None) -> int | None:
        handler_id = getattr(assignment, "handler_id", None)
        if handler_id is None:
            return None
        handler = await self._handlers.get(handler_id)
        limit = getattr(handler, "authority_limit_minor", None) if handler else None
        return int(limit) if limit is not None else None

    async def _authority_currency(self, assignment: object | None) -> str | None:
        """The money the assigned handler's limit is denominated in.

        Read alongside the limit rather than assumed to match the claim, because it
        frequently does not: the directory is kept in the desk's own currency and a
        claim is booked in the loss's. `approval_blocks` refuses rather than guesses
        when the two differ.
        """
        handler_id = getattr(assignment, "handler_id", None)
        if handler_id is None:
            return None
        handler = await self._handlers.get(handler_id)
        return getattr(handler, "currency", None) if handler else None

    async def _refresh_authority(self, claim: Claim) -> None:
        """Re-derive `over_authority` after the reserve moves.

        The flag is set at triage against the reserve as it then stood, and a
        movement is exactly the event that can invalidate it. Left stale, a claim
        raised past its handler's limit would keep a green decision bar — which is
        the one direction this flag must not be wrong in.
        """
        assignment = await self._claims.get_assignment(claim.id)
        limit = await self._authority_limit(assignment)
        if limit is None:
            return
        claim.over_authority = (claim.reserve_minor or 0) > limit


# ---------------------------------------------------------------------------
# Phrasing
# ---------------------------------------------------------------------------

_SECTION_PHRASE: dict[NoteSection, str] = {
    NoteSection.GENERAL: "the claim",
    NoteSection.INSPECTION: "the inspection",
    NoteSection.ASSESSMENT: "the assessment",
    NoteSection.FRAUD: "the fraud review",
    NoteSection.RECOVERY: "recoveries",
}


def _section_phrase(section: NoteSection) -> str:
    return _SECTION_PHRASE.get(section, "the claim")


def _movement_phrase(movement_type: MovementType, amount_minor: int) -> str:
    if movement_type is MovementType.RECOVERY:
        return "recorded a recovery of"
    verb = "raised" if amount_minor > 0 else "released"
    return f"{verb} the {movement_type!s} reserve by"


def _signed(amount_minor: int) -> str:
    return f"{amount_minor:+d}"


def _sentence_list(items: list[str]) -> str:
    """The blocks as a sentence a handler can act on, in the order to act in.

    "First, assign a handler, then refer the settlement" reads as instructions.
    "assign a handler; refer the settlement" reads as a validation dump, which is
    what the API returned before this existed.
    """
    if len(items) == 1:
        return f"First, {items[0]}"
    if len(items) == 2:
        return f"First, {items[0]}, then {items[1]}"
    return "First, " + ", ".join(items[:-1]) + f", then {items[-1]}"


__all__ = ["ClaimCaseworkService"]
