"""The claim state machine, and the rules a decision is taken under.

`lifecycle.py` is the notification's equivalent, and this is deliberately its
mirror: the same three jobs, stated the same way, so a reader who knows one knows
the other. Four rules live here and nowhere else:

* which transitions are legal (`can_transition`), so an out-of-order call is a
  409 rather than a corrupt row;
* which status a decision implies (`status_for_decision`), so the verb a handler
  pressed and the status the claim lands on are decided in one place rather than
  at each call site;
* what stands between a handler and a settlement (`approval_blocks`), which the
  workbench renders and the decision endpoint enforces — from the same function,
  because a screen that lists three blockers and an endpoint that checks two is
  worse than either alone; and
* which part of the claim an audit event belongs to (`activity_category`), so the
  activity log groups by subject rather than by whichever service happened to
  write the row.

Everything here is a pure function over values. No session, no ORM instance
beyond attribute reads, and no clock — the same argument `matching.py` makes:
it is arithmetic and rules, it has to be reproducible, and an auditor has to be
able to follow it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.domain.enums import (
    COST_MOVEMENT_TYPES,
    TERMINAL_CLAIM_STATUSES,
    ActivityCategory,
    AssignmentStatus,
    AuditEventType,
    ClaimDecisionAction,
    ClaimStatus,
)

# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

#: Legal transitions. Absent keys are terminal.
#:
#: `IN_REVIEW` lists itself, which no other state does. That is what
#: `REQUEST_INFORMATION` needs: asking a broker for a missing schedule is a real
#: decision with a real audit event, and it leaves the claim exactly where it was.
#: Modelling it as a no-op transition rather than as "a decision that sometimes
#: does not transition" is what keeps `status_for_decision` total.
_ALLOWED: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.FNOL: frozenset(
        {
            ClaimStatus.CLASSIFIED,
            ClaimStatus.IN_REVIEW,
            ClaimStatus.ESCALATED,
            ClaimStatus.REJECTED,
        }
    ),
    ClaimStatus.CLASSIFIED: frozenset(
        {
            ClaimStatus.IN_REVIEW,
            ClaimStatus.ESCALATED,
            ClaimStatus.REJECTED,
        }
    ),
    ClaimStatus.IN_REVIEW: frozenset(
        {
            ClaimStatus.IN_REVIEW,
            ClaimStatus.ESCALATED,
            ClaimStatus.APPROVED,
            ClaimStatus.REJECTED,
        }
    ),
    #: An escalated claim can come back down. A manager who looks at a referral
    #: and decides it did not need them has to be able to put it back on the
    #: handler's desk, and a state machine that only ratchets upward turns every
    #: unnecessary referral into a settled or declined claim.
    ClaimStatus.ESCALATED: frozenset(
        {
            ClaimStatus.IN_REVIEW,
            ClaimStatus.ESCALATED,
            ClaimStatus.APPROVED,
            ClaimStatus.REJECTED,
        }
    ),
}


def is_terminal(status: str) -> bool:
    return status in TERMINAL_CLAIM_STATUSES


def can_transition(current: str, target: str) -> bool:
    """Whether the claim may move from `current` to `target`.

    Unknown statuses answer `False` rather than raising. A row written by an older
    revision than this code is a thing to refuse to move, not a thing to crash on.
    """
    try:
        source = ClaimStatus(current)
        destination = ClaimStatus(target)
    except ValueError:
        return False
    return destination in _ALLOWED.get(source, frozenset())


def allowed_transitions(current: str) -> frozenset[ClaimStatus]:
    """Where the claim may go from here. Empty for a terminal or unknown status."""
    try:
        return _ALLOWED.get(ClaimStatus(current), frozenset())
    except ValueError:
        return frozenset()


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

#: What each verb means for the claim's status.
#:
#: `SEND_TO_APPROVAL` and `REFER_TO_MANAGER` both land on `ESCALATED`, and they
#: are still two verbs: the first says "I have finished and somebody senior must
#: sign it", the second says "I cannot finish this". They are told apart in the
#: audit event's context, not by the status they produce.
_STATUS_FOR_DECISION: dict[ClaimDecisionAction, ClaimStatus] = {
    ClaimDecisionAction.APPROVE_SETTLEMENT: ClaimStatus.APPROVED,
    ClaimDecisionAction.SEND_TO_APPROVAL: ClaimStatus.ESCALATED,
    ClaimDecisionAction.REFER_TO_MANAGER: ClaimStatus.ESCALATED,
    ClaimDecisionAction.REQUEST_INFORMATION: ClaimStatus.IN_REVIEW,
    ClaimDecisionAction.RETURN_TO_HANDLER: ClaimStatus.IN_REVIEW,
    ClaimDecisionAction.DECLINE: ClaimStatus.REJECTED,
}

#: Decisions that end the claim's casework. Only these are gated by
#: `approval_blocks` — asking for information or referring upward is always
#: available, and gating them would trap a claim nobody can move.
COMMITTING_DECISIONS = frozenset(
    {ClaimDecisionAction.APPROVE_SETTLEMENT, ClaimDecisionAction.DECLINE}
)

#: Decisions that hand the claim to somebody else, and so are worth interrupting a
#: manager for.
#:
#: The two that land on `ESCALATED`. Asking for information is not here: it leaves
#: the claim with the handler who asked, and the person who needs to act on it is
#: outside this system entirely. Approving and declining end the claim — there is
#: nobody left to tell.
ESCALATING_DECISIONS = frozenset(
    {ClaimDecisionAction.REFER_TO_MANAGER, ClaimDecisionAction.SEND_TO_APPROVAL}
)

#: How each verb reads in the trail. Present tense, third person, because that is
#: how the rest of the audit summaries are written.
_DECISION_PHRASE: dict[ClaimDecisionAction, str] = {
    ClaimDecisionAction.APPROVE_SETTLEMENT: "approved the settlement",
    ClaimDecisionAction.SEND_TO_APPROVAL: "sent the claim for approval",
    ClaimDecisionAction.REFER_TO_MANAGER: "referred the claim to a manager",
    ClaimDecisionAction.REQUEST_INFORMATION: "requested further information",
    ClaimDecisionAction.RETURN_TO_HANDLER: "returned the claim to its handler",
    ClaimDecisionAction.DECLINE: "declined the claim",
}


def status_for_decision(decision: ClaimDecisionAction) -> ClaimStatus:
    return _STATUS_FOR_DECISION[decision]


def decision_phrase(decision: ClaimDecisionAction) -> str:
    return _DECISION_PHRASE[decision]


def decision_summary(decision: ClaimDecisionAction, *, actor: str) -> str:
    """The audit summary for a decision. One sentence, attributed."""
    return f"{actor} {_DECISION_PHRASE[decision]}."


# ---------------------------------------------------------------------------
# What blocks a settlement
# ---------------------------------------------------------------------------


class BlockCode(StrEnum):
    """Which block, in a form a screen can act on.

    The sentence is for the handler; the code is for the interface. Without it a
    client wanting to offer "assign a handler" beside the block that says so has to
    match on prose — which breaks the first time the wording is improved, and which
    is why every other refusal in this codebase carries a code.
    """

    #: Fraud indicators nobody has decided about. Keeps its old key so a client
    #: matching on the string keeps working; what changed is what raises it.
    FRAUD_FLAG = "fraud_flag"
    #: An SIU case still running. Distinct from the indicators: a claim can have
    #: every flag disposed and an investigation still open, and settling underneath
    #: an open investigation is the thing this stops.
    SIU_OPEN = "siu_open"
    OVER_AUTHORITY = "over_authority"
    UNASSIGNED = "unassigned"
    #: The claim is already decided. Unlike the other three this is not something
    #: anybody can clear from the workbench, which is why the bar renders it as a
    #: state rather than as a to-do.
    DECIDED = "decided"


@dataclass(slots=True, frozen=True)
class ApprovalBlock:
    """One thing standing between this claim and a settlement."""

    code: BlockCode
    #: An imperative, because the list is read as a to-do rather than a diagnosis.
    reason: str


def approval_blocks(
    claim: object,
    *,
    assignment: object | None,
    authority_limit_minor: int | None = None,
    authority_currency: str | None = None,
    incurred_minor: int | None = None,
    outstanding_indicators: int = 0,
    siu_open: bool = False,
) -> list[ApprovalBlock]:
    """What stands between this claim and a settlement, in the handler's words.

    Rendered by the workbench above the decision bar and enforced by the decision
    endpoint. One function for both, because the alternative — a screen listing
    three blockers and an endpoint checking two — produces the worst outcome
    available: a handler who presses a button the screen said was safe and gets a
    422 that names a reason the screen never mentioned. Which is exactly what
    happened for a *decided* claim until `BlockCode.DECIDED` existed.

    Phrased as imperatives ("assign a handler") rather than as conditions
    ("unassigned"), because the list is read as a to-do rather than as a diagnosis.

    `authority_limit_minor` is the assigned handler's limit, fetched by the caller
    the way `_detail` already fetches it. It is **ignored unless the claim is
    actually assigned**, which is the one safety property this signature has to
    carry: passing the limit of somebody who is not on the file must not be able to
    clear the block, and the unassigned check catches the case where nobody is.

    `incurred_minor` is the ledger's own total where the caller has it. Absent, the
    claim's held reserve is used. The distinction matters as soon as payments exist:
    a claim reserved at £40,000 that has already paid out £45,000 is over a £50,000
    authority on what it has *incurred*, even though its reserve says otherwise.

    **The fraud block reads the review, not the flag.** `outstanding_indicators` is
    how many fraud indicators nobody has decided about and `siu_open` is whether an
    SIU case is still running; the block is on those. It used to be on
    `claims.fraud_flag`, and that was a dead end rather than a stricter rule:
    `fraud_flag` is written in exactly one place — triage, from
    `TriageCategory.FRAUD_REVIEW` — and **nothing in the product ever cleared it**.
    A handler who accepted every red flag on the claim wrote rows to
    `claim_fraud_dispositions`, which the block did not read, and was told to "clear
    the fraud review flag" by a screen offering no way to clear it. The claim could
    not be approved by anybody, ever.

    The flag keeps its job as the machine's signal — it drives the fraud queue
    filter and the score. What it stops being is a gate with no key. This is the
    same move `siu_status` already made when it stopped being derived from the flag
    and became a record.

    Both default to "nothing outstanding" so a caller that does not know about fraud
    at all — the approval sheet's authority preview, a test constructing one claim —
    is not silently blocked by a figure it never passed.

    **`authority_currency` is not optional information.** The two figures being
    compared are money, and comparing minor units across currencies is comparing
    nothing: a claim reserved at 370,000,000 USD-minor against a limit of 25,000,000
    GBP-minor happens to answer correctly, and a claim at 40,000,000 GBP-minor
    against 50,000,000 USD-minor answers *wrongly* — it clears a $500,000 authority
    with a £400,000 exposure. When the currencies differ this **blocks**, because
    the rate that would relate them is a fact this system does not hold, and the
    safe direction on the field that decides whether money leaves is to refuse a
    settlement nobody can prove is within authority.
    """
    #: **First, and returning on its own.** `decide` refuses a decided claim before
    #: it looks at anything else, and this function is documented as the one list
    #: the screen and the endpoint share — so a decided claim reporting no blocks
    #: was that guarantee quietly broken. It is what left an *Approve settlement*
    #: button live on a claim that had just been approved: no blocks, so the bar
    #: read it as "go ahead", and the API answered 409.
    #:
    #: Alone, because the others are things to go and do and this is not. Telling
    #: somebody to clear a fraud flag on a claim that closed last week is noise.
    status = str(getattr(claim, "status", "") or "")
    if is_terminal(status):
        return [ApprovalBlock(BlockCode.DECIDED, f"it has already been {status.replace('_', ' ')}")]

    blocks: list[ApprovalBlock] = []

    #: Outstanding *work*, not a standing flag. Phrased with the count because "two
    #: fraud indicators" is a to-do a handler can finish, where "the fraud review
    #: flag" named a thing no screen could act on.
    if outstanding_indicators > 0:
        blocks.append(
            ApprovalBlock(
                BlockCode.FRAUD_FLAG,
                "decide the open fraud indicator"
                if outstanding_indicators == 1
                else f"decide the {outstanding_indicators} open fraud indicators",
            )
        )

    if siu_open:
        blocks.append(
            ApprovalBlock(BlockCode.SIU_OPEN, "close the SIU investigation, or record its outcome")
        )

    assigned = (
        assignment is not None and getattr(assignment, "status", None) == AssignmentStatus.ASSIGNED
    )
    exposure = incurred_minor if incurred_minor is not None else getattr(claim, "reserve_minor", 0)
    limit = authority_limit_minor if assigned else None

    #: Both sides of the comparison, in the money each is actually in. A missing
    #: currency on either side is treated as agreeing — every seeded row carries
    #: one, and refusing every settlement on a null would be a worse failure than
    #: the one this guards against.
    claim_currency = (getattr(claim, "currency", None) or "").upper()
    limit_currency = (authority_currency or "").upper()
    incomparable = bool(
        limit is not None and claim_currency and limit_currency and claim_currency != limit_currency
    )

    if incomparable:
        blocks.append(
            ApprovalBlock(
                BlockCode.OVER_AUTHORITY,
                f"refer the settlement — this claim is in {claim_currency} and the "
                f"authority on it is in {limit_currency}, so it cannot be checked here",
            )
        )
    elif getattr(claim, "over_authority", False) or (limit is not None and exposure > limit):
        blocks.append(
            ApprovalBlock(
                BlockCode.OVER_AUTHORITY, "refer the settlement — it is above your authority"
            )
        )

    if not assigned:
        blocks.append(ApprovalBlock(BlockCode.UNASSIGNED, "assign a handler"))

    return blocks


def blocks_for_decision(
    decision: ClaimDecisionAction,
    claim: object,
    *,
    assignment: object | None,
    authority_limit_minor: int | None = None,
    authority_currency: str | None = None,
    incurred_minor: int | None = None,
    outstanding_indicators: int = 0,
    siu_open: bool = False,
) -> list[ApprovalBlock]:
    """The blocks that actually apply to this verb.

    Empty for a non-committing one — asking for information or referring upward is
    always available, and gating them would trap a claim nobody can move.

    **Except on a decided claim, where every verb is refused.** `decide` checks
    `is_terminal` before it checks anything else, so requesting information on a
    closed claim fails too, and a list that said otherwise would send a screen at a
    button the endpoint will not honour.
    """
    if is_terminal(str(getattr(claim, "status", "") or "")):
        return approval_blocks(
            claim,
            assignment=assignment,
            authority_limit_minor=authority_limit_minor,
            authority_currency=authority_currency,
            incurred_minor=incurred_minor,
            outstanding_indicators=outstanding_indicators,
            siu_open=siu_open,
        )
    if decision not in COMMITTING_DECISIONS:
        return []
    return approval_blocks(
        claim,
        assignment=assignment,
        authority_limit_minor=authority_limit_minor,
        authority_currency=authority_currency,
        incurred_minor=incurred_minor,
        outstanding_indicators=outstanding_indicators,
        siu_open=siu_open,
    )


# ---------------------------------------------------------------------------
# The activity log
# ---------------------------------------------------------------------------

#: Which part of the claim each audit event type belongs to.
#:
#: Exhaustive over `AuditEventType` by intent, and `activity_category` falls back
#: rather than raising on a value this table has not caught up with — a new event
#: type should appear in the log under a reasonable heading, not break the tab
#: that renders it.
_CATEGORY_FOR_EVENT: dict[str, ActivityCategory] = {
    # Intake — the notice arriving and being worked.
    AuditEventType.FNOL_CREATED: ActivityCategory.INTAKE,
    AuditEventType.FNOL_INGESTED: ActivityCategory.INTAKE,
    AuditEventType.FNOL_UPDATED: ActivityCategory.INTAKE,
    AuditEventType.FNOL_STATUS_CHANGED: ActivityCategory.INTAKE,
    AuditEventType.FNOL_DELETED: ActivityCategory.INTAKE,
    AuditEventType.PIPELINE_STARTED: ActivityCategory.INTAKE,
    AuditEventType.PIPELINE_COMPLETED: ActivityCategory.INTAKE,
    AuditEventType.PIPELINE_FAILED: ActivityCategory.INTAKE,
    AuditEventType.CLAIM_CREATED: ActivityCategory.INTAKE,
    # Documents.
    AuditEventType.DOCUMENT_UPLOADED: ActivityCategory.DOCUMENTS,
    AuditEventType.DOCUMENT_DELETED: ActivityCategory.DOCUMENTS,
    # Review — the reading of the file, and every correction to it.
    AuditEventType.EXTRACTION_COMPLETED: ActivityCategory.REVIEW,
    AuditEventType.FIELD_CHANGED: ActivityCategory.REVIEW,
    AuditEventType.POLICY_SELECTED: ActivityCategory.REVIEW,
    AuditEventType.POLICY_IDENTIFIED: ActivityCategory.REVIEW,
    AuditEventType.POLICY_REFERRED: ActivityCategory.REVIEW,
    AuditEventType.DUPLICATE_RESOLVED: ActivityCategory.REVIEW,
    AuditEventType.CLASSIFICATION_OVERRIDDEN: ActivityCategory.REVIEW,
    AuditEventType.EXCEPTION_RESOLVED: ActivityCategory.REVIEW,
    # Assessment — grading the loss, routing it, deciding it.
    AuditEventType.SEVERITY_OVERRIDDEN: ActivityCategory.ASSESSMENT,
    AuditEventType.TRIAGE_COMPLETED: ActivityCategory.ASSESSMENT,
    AuditEventType.TRIAGE_OVERRIDDEN: ActivityCategory.ASSESSMENT,
    AuditEventType.HANDLER_ASSIGNED: ActivityCategory.ASSESSMENT,
    AuditEventType.CLAIM_STATUS_CHANGED: ActivityCategory.ASSESSMENT,
    AuditEventType.CLAIM_DECIDED: ActivityCategory.ASSESSMENT,
    AuditEventType.CLAIM_DECISION_BLOCKED: ActivityCategory.ASSESSMENT,
    # The visit. Mapped explicitly rather than left to the `claim.*` fallback,
    # which would file all six under assessment and leave the log's own inspection
    # chip permanently empty.
    AuditEventType.INSPECTION_COMMISSIONED: ActivityCategory.INSPECTION,
    AuditEventType.INSPECTION_SCHEDULED: ActivityCategory.INSPECTION,
    AuditEventType.INSPECTION_ATTENDED: ActivityCategory.INSPECTION,
    AuditEventType.INSPECTION_STATUS_CHANGED: ActivityCategory.INSPECTION,
    AuditEventType.INSPECTION_OBSERVED: ActivityCategory.INSPECTION,
    AuditEventType.INSPECTION_ACTION_SET: ActivityCategory.INSPECTION,
    AuditEventType.INSPECTION_FILED: ActivityCategory.INSPECTION,
    # Recoveries and the SIU case, mapped explicitly for the reason the visit's six
    # are: the `claim.*` fallback would file them under assessment and leave both
    # chips on the log permanently empty.
    AuditEventType.RECOVERY_OPENED: ActivityCategory.RECOVERY,
    AuditEventType.RECOVERY_PROGRESSED: ActivityCategory.RECOVERY,
    AuditEventType.RECOVERY_TASK_SET: ActivityCategory.RECOVERY,
    AuditEventType.RECOVERY_DECLINED: ActivityCategory.RECOVERY,
    AuditEventType.RECOVERY_RECONSIDERED: ActivityCategory.RECOVERY,
    AuditEventType.SIU_REFERRED: ActivityCategory.FRAUD,
    AuditEventType.SIU_STATUS_CHANGED: ActivityCategory.FRAUD,
    AuditEventType.FRAUD_INDICATOR_DISPOSED: ActivityCategory.FRAUD,
    # The money.
    AuditEventType.RESERVE_MOVED: ActivityCategory.FINANCIAL,
    AuditEventType.PAYMENT_RECORDED: ActivityCategory.FINANCIAL,
    AuditEventType.DEDUCTIBLE_APPLIED: ActivityCategory.FINANCIAL,
    # Fraud, and what comes back.
    AuditEventType.FRAUD_REVIEWED: ActivityCategory.FRAUD,
    AuditEventType.RECOVERY_RECORDED: ActivityCategory.RECOVERY,
    # Notes, from either side of the claim boundary.
    AuditEventType.NOTE_ADDED: ActivityCategory.NOTE,
    AuditEventType.CLAIM_NOTE_ADDED: ActivityCategory.NOTE,
}

#: Catastrophe attribution is a finding about the incident rather than about the
#: money or the paperwork, so it reads under review with the rest of the pipeline's
#: conclusions. Called out separately only because the pair is easy to miss above.
_CATEGORY_FOR_EVENT[AuditEventType.CAT_MATCH_CONFIRMED] = ActivityCategory.REVIEW
_CATEGORY_FOR_EVENT[AuditEventType.CAT_MATCH_REMOVED] = ActivityCategory.REVIEW

#: The events a handler skims the log for. Everything else is available and
#: nothing else is highlighted — a log where every line is material is a log
#: nobody scans.
MATERIAL_EVENTS = frozenset(
    {
        AuditEventType.CLAIM_CREATED,
        AuditEventType.CLAIM_DECIDED,
        AuditEventType.CLAIM_STATUS_CHANGED,
        AuditEventType.RESERVE_MOVED,
        AuditEventType.PAYMENT_RECORDED,
        AuditEventType.RECOVERY_RECORDED,
        AuditEventType.HANDLER_ASSIGNED,
        AuditEventType.FRAUD_REVIEWED,
        AuditEventType.POLICY_SELECTED,
        AuditEventType.TRIAGE_OVERRIDDEN,
        AuditEventType.SEVERITY_OVERRIDDEN,
        #: Two of the six, and only two. Instructing an adjuster and their report
        #: coming back are what a handler skims for; a photograph count being
        #: corrected is not, and a log where every line is material is a log
        #: nobody scans.
        AuditEventType.INSPECTION_COMMISSIONED,
        AuditEventType.INSPECTION_ATTENDED,
        #: The report arriving is what the handler has been waiting for.
        AuditEventType.INSPECTION_FILED,
        #: Money actually coming back, and a referral being made. Both change what
        #: the claim costs or who owns it; the intermediate steps of each do not.
        AuditEventType.RECOVERY_RECORDED,
        AuditEventType.SIU_REFERRED,
    }
)


def activity_category(event_type: str) -> ActivityCategory:
    """Which chip on the activity log this event sits under.

    Falls back on the event type's own prefix before giving up, so a new
    `fnol.*` event lands in intake and a new `claim.*` event lands in assessment
    rather than both landing in "notes" for want of a table entry.
    """
    known = _CATEGORY_FOR_EVENT.get(event_type)
    if known is not None:
        return known
    if event_type.startswith("fnol."):
        return ActivityCategory.INTAKE
    return ActivityCategory.ASSESSMENT


def is_material(event_type: str) -> bool:
    return event_type in MATERIAL_EVENTS


# ---------------------------------------------------------------------------
# The ledger's arithmetic
# ---------------------------------------------------------------------------


class Movement(Protocol):
    """What the arithmetic below needs a movement to be.

    A structural type rather than an import of `ClaimReserveMovement`, so this
    module stays free of the ORM and the tests that exercise the arithmetic can
    pass three-field stand-ins instead of constructing mapped rows against a
    session that does not exist.
    """

    movement_type: str
    amount_minor: int


def held_by_movement_type(movements: Iterable[Movement]) -> dict[str, int]:
    """What is currently held on each movement type, as the sum of its deltas.

    A movement is a signed delta rather than a new total, which is what makes this
    a sum rather than a last-write-wins read of whichever row happens to be newest.
    The consequence worth stating: this function is the *definition* of the held
    figure, and `claims.reserve_minor` is a cache of its total that the write path
    keeps in step. If the two ever disagree, this one is right.
    """
    totals: dict[str, int] = {}
    for movement in movements:
        head = str(movement.movement_type)
        totals[head] = totals.get(head, 0) + int(movement.amount_minor or 0)
    return totals


def previous_held(movements: Sequence[Movement], *, movement_type: str) -> int:
    """What was held on this movement type before its most recent movement.

    The figure the financials tab shows beside the current one, so the change is
    legible without the handler doing the subtraction. Movements are expected
    newest-first, which is the order every read in this module returns them in.
    """
    matching = [m for m in movements if str(m.movement_type) == movement_type]
    if not matching:
        return 0
    return sum(int(m.amount_minor or 0) for m in matching[1:])


def incurred_minor(movements: Iterable[Movement]) -> int:
    """What the claim has cost so far: indemnity plus expense plus legal.

    Recoveries are excluded rather than netted off. A recovery that has been
    identified but not banked would otherwise reduce the figure an authority limit
    is checked against, which is the one place optimism is most expensive.
    """
    return sum(
        int(movement.amount_minor or 0)
        for movement in movements
        if str(movement.movement_type) in COST_MOVEMENT_TYPES
    )


__all__ = [
    "COMMITTING_DECISIONS",
    "MATERIAL_EVENTS",
    "ApprovalBlock",
    "BlockCode",
    "Movement",
    "activity_category",
    "allowed_transitions",
    "approval_blocks",
    "blocks_for_decision",
    "can_transition",
    "decision_phrase",
    "decision_summary",
    "held_by_movement_type",
    "incurred_minor",
    "is_material",
    "is_terminal",
    "previous_held",
    "status_for_decision",
]
