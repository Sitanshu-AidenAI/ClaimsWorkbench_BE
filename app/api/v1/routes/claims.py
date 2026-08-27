"""Claims endpoints.

The other half of the FNOL journey: a claim created from a notification has to
turn up somewhere a handler works, and this is that somewhere. The queue, the
claim record and the document-review workspace all read from the claim and the
notification behind it, so nothing an officer established at intake is lost or
re-entered.

Assignment sits here rather than under `/fnol` because it is a claims manager's
decision about a claim, not an intake officer's about a notice — and the roles
that gate it say so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps.auth import require_roles
from app.api.deps.services import FNOLContext, FNOLContextDep
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain import claim_lifecycle
from app.domain.enums import (
    CLAIM_ASSIGN_ROLES,
    CLAIM_WORK_ROLES,
    FNOL_READ_ROLES,
    AnalysisKind,
    AuditEventType,
    ClaimStatus,
)
from app.domain.knowledge_graph import GraphInput, build_claim_graph
from app.domain.money import to_currency, to_major
from app.models.claim import Claim
from app.schemas import claim_sections as sections_api
from app.schemas import claims as api
from app.schemas import knowledge_graph as kg_api
from app.schemas.fnol import (
    Money,
    to_assignment,
    to_audit_event,
    to_document,
    to_note,
    to_triage,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/claims", tags=["claims"])

ReadAccess = Annotated[Principal, Depends(require_roles(*FNOL_READ_ROLES))]
AssignAccess = Annotated[Principal, Depends(require_roles(*CLAIM_ASSIGN_ROLES))]
#: Working the claim — notes, the reserve, decisions. Wider than `AssignAccess`,
#: because a handler works their own file; narrower than `ReadAccess`, because an
#: intake officer's job ends when the notice becomes a claim.
WorkAccess = Annotated[Principal, Depends(require_roles(*CLAIM_WORK_ROLES))]

#: The queue's status chips, in lifecycle order rather than alphabetically.
_STATUS_LABELS: tuple[tuple[str, str], ...] = (
    (ClaimStatus.FNOL, "FNOL"),
    (ClaimStatus.CLASSIFIED, "Classified"),
    (ClaimStatus.IN_REVIEW, "In review"),
    (ClaimStatus.ESCALATED, "Escalated"),
    (ClaimStatus.APPROVED, "Approved"),
    (ClaimStatus.REJECTED, "Rejected"),
)

#: How the workbench's stepper maps onto claim status.
_STAGE_FOR_STATUS = {
    ClaimStatus.FNOL: "fnol_received",
    ClaimStatus.CLASSIFIED: "classified",
    ClaimStatus.IN_REVIEW: "in_review",
    ClaimStatus.ESCALATED: "decision",
    ClaimStatus.APPROVED: "closed",
    ClaimStatus.REJECTED: "closed",
}


@router.get("", response_model=api.ClaimsQueueResult, summary="The claims queue")
async def list_claims(
    context: FNOLContextDep,
    principal: ReadAccess,
    scope: Annotated[str, Query(pattern="^(visible|assigned|reported)$")] = "visible",
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    metric: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> api.ClaimsQueueResult:
    """A page of claims, with the figures and chips that describe the same set.

    `scope` narrows against the signed-in principal on the server rather than the
    client filtering what it was sent — "assigned to me" has to mean the server's
    idea of me.

    And *me* is a **subject**, not a name. `assigned` resolves through the claim's
    assignment to the handler directory row carrying the reader's `sub`, because the
    identity provider's display name and the directory's `full_name` are two strings
    nothing keeps in step. While this compared them, a claim assigned to a real
    person vanished from the manager's queue and turned up on nobody else's.
    """
    actor = _actor(principal)
    statuses = [status_filter] if status_filter and status_filter != "all" else None

    rows, total = await context.claims.list_queue(
        statuses=statuses,
        # By account rather than by display name. The two are not the same string
        # and the difference is why an assigned claim used to reach nobody's queue
        # — see `ClaimRepository._filtered`.
        handler_subject=principal.subject if scope == "assigned" else None,
        created_by=actor if scope == "reported" else None,
        fraud_only=metric == "fraud_review",
        cat_only=metric == "cat_linked",
        search=search,
        limit=page_size,
        offset=(page - 1) * page_size,
    )

    status_counts = await context.claims.status_counts()
    all_count = sum(status_counts.values())
    exposure = await context.claims.open_exposure_minor()

    return api.ClaimsQueueResult(
        items=[_summary(claim) for claim in rows],
        total=total,
        page=page,
        page_size=page_size,
        metrics=[
            api.QueueMetric(
                id="open_exposure",
                label="Open exposure",
                display=_compact(exposure, "GBP"),
                count=None,
            ),
            api.QueueMetric(
                id="fraud_review",
                label="Fraud review",
                display=str(await context.claims.count_where(fraud_only=True)),
                count=await context.claims.count_where(fraud_only=True),
            ),
            api.QueueMetric(
                id="over_authority",
                label="Over authority",
                display=str(await context.claims.count_where(over_authority=True)),
                count=await context.claims.count_where(over_authority=True),
            ),
            api.QueueMetric(
                id="cat_linked",
                label="CAT linked",
                display=str(await context.claims.count_where(cat_only=True)),
                count=await context.claims.count_where(cat_only=True),
            ),
        ],
        facets=[
            api.StatusFacet(status="all", label="All", count=all_count),
            *(
                api.StatusFacet(status=value, label=label, count=status_counts.get(value, 0))
                for value, label in _STATUS_LABELS
            ),
        ],
        scope_summary=f"{all_count} claims created from intake",
    )


@router.get("/handlers", response_model=list[api.HandlerOut], summary="Available handlers")
async def list_handlers(context: FNOLContextDep, principal: ReadAccess) -> list[api.HandlerOut]:
    del principal
    return [api.to_handler(handler) for handler in await context.handlers.list_available()]


@router.get("/{reference}", response_model=api.ClaimDetail, summary="One claim")
async def get_claim(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> api.ClaimDetail:
    del principal
    claim = await _load(context, reference)
    return await _detail(context, claim)


@router.get(
    "/{reference}/workbench",
    response_model=api.ClaimWorkbench,
    summary="The document-review workspace",
)
async def get_workbench(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> api.ClaimWorkbench:
    """Everything the workbench reads, from the claim and its notification.

    One call rather than five, for the reason the FNOL detail is one call: the
    panels are decided on together, and five endpoints would let the rules
    disagree with the coverage assessment they were run against.
    """
    del principal
    claim = await _load(context, reference)
    case = await context.cases.get(claim.fnol_case_id) if claim.fnol_case_id else None

    documents = await context.cases.list_documents(case.id) if case else []
    fields = await context.cases.list_fields(case.id) if case else []
    exceptions = await context.cases.list_exceptions(case.id) if case else []
    coverage = await _coverage(context, case)
    fraud = await _fraud(context, case)
    triage_row = await context.claims.get_triage(claim.id)
    assignment_row = await context.claims.get_assignment(claim.id)

    by_document: dict[str, list[api.ClaimExtractedFieldOut]] = {}
    for field in fields:
        if field.value_text is None:
            continue
        key = str(field.source_document_id) if field.source_document_id else "notification"
        by_document.setdefault(key, []).append(
            api.ClaimExtractedFieldOut(
                id=field.field_path,
                label=field.label,
                value=field.value_text,
                confidence=float(field.confidence or 0.0),
                numeric=field.section in {"financial", "loss"},
                corrected=bool(field.human_modified),
                source_document_id=field.source_document_id,
            )
        )

    # The same function `POST /{reference}/decision` enforces, so the list above the
    # decision bar and the list the endpoint refuses on cannot drift apart. It was
    # three inline conditions here before, which is exactly how they would have.
    handler = (
        await context.handlers.get(assignment_row.handler_id)
        if assignment_row and assignment_row.handler_id
        else None
    )
    movements = await context.claims.list_movements(claim.id)
    blocks = claim_lifecycle.approval_blocks(
        claim,
        assignment=assignment_row,
        authority_limit_minor=handler.authority_limit_minor if handler else None,
        authority_currency=handler.currency if handler else None,
        incurred_minor=claim_lifecycle.incurred_minor(movements) if movements else None,
    )

    return api.ClaimWorkbench(
        reference=claim.reference,
        status=claim.status,
        summary_line=" · ".join(
            part
            for part in (
                (claim.loss_type or "").replace("_", " ").title() or None,
                claim.loss_location,
                f"Loss {claim.date_of_loss:%d %b %Y}" if claim.date_of_loss else None,
            )
            if part
        ),
        policyholder=claim.insured_name or claim.claimant_name or "—",
        policy_reference=claim.policy_number or "—",
        reserve=Money(amount_minor=claim.reserve_minor or 0, currency=claim.currency),
        estimate=Money(amount_minor=claim.reserve_minor or 0, currency=claim.currency),
        severity=claim.severity,
        stage=_STAGE_FOR_STATUS.get(claim.status, "in_review"),
        stage_note=_stage_note(claim, assignment_row),
        siu_referral_open=bool(claim.fraud_flag),
        approval_blocks=[
            api.ApprovalBlockOut(code=str(block.code), reason=block.reason) for block in blocks
        ],
        documents=[to_document(document) for document in documents],
        extracted_fields=by_document,
        coverage=coverage,
        rules=[
            api.ClaimRuleOut(
                code=exception.code.upper().replace("_", "-")[:12],
                label=exception.title,
                outcome="warn" if exception.status == "open" else "pass",
                detail=exception.detail if exception.status == "open" else None,
            )
            for exception in exceptions
        ],
        fraud=fraud,
        triage=to_triage(triage_row) if triage_row else None,
        assignment=to_assignment(assignment_row) if assignment_row else None,
        fnol_reference=case.reference if case else None,
    )


@router.get(
    "/{reference}/sections",
    response_model=sections_api.ClaimSections,
    summary="The workbench's six operational sections",
)
async def get_sections(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> sections_api.ClaimSections:
    """The inspection, assessment, financials, fraud, recoveries and activity log.

    Separate from `/workbench` rather than folded into it, and the reason is worth
    stating: the aggregate above is what the *document-processing* side of the
    product models — extraction, rules, coverage verdict, confidence — and this is
    what a *claims* system owns around it. Two endpoints means a handler ticking
    something on the assessment tab does not cost them the document viewer's place.

    **All six sections are built**, which they were not for most of this module's
    life. The field inspection became real with `claim_inspections`, and the recovery
    register and the SIU case with `claim_recoveries` and `claim_siu_cases` — the
    last two to go. `available` is now true on every one; the assessment is the only
    section that still carries an `unavailable_reason` beside it, naming the priced
    damage breakdown it lacks rather than disowning the whole tab.

    See `app.services.claims.sections` for what is behind each one. Nothing in it is
    fixture data.
    """
    del principal
    claim = await _load(context, reference)
    return await context.sections.build(claim)


@router.post(
    "/{reference}/notes",
    response_model=sections_api.SectionNoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Write a note on the claim",
)
async def add_claim_note(
    reference: str,
    payload: api.ClaimNoteRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.SectionNoteOut:
    """A note on the claim, filed against the tab it was written on.

    Distinct from `POST /fnol/{reference}/notes`, which is a note on the *notice*.
    Both exist and both should: an intake officer's note about a broker's email and
    a handler's note about an adjuster's visit are different records, and a single
    table would put the first into a settlement audit.
    """
    claim = await _load(context, reference)
    note = await context.casework.add_note(
        claim, body=payload.body, section=payload.section, actor=_actor(principal)
    )
    await context.commit()
    return sections_api.SectionNoteOut(
        id=note.id, author=note.author, written_at=note.created_at, body=note.body
    )


@router.post(
    "/{reference}/reserve",
    response_model=sections_api.ClaimFinancialsOut,
    summary="Move the reserve",
)
async def move_reserve(
    reference: str,
    payload: api.ReserveMovementRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimFinancialsOut:
    """Append a movement to the ledger.

    `amount_minor` is a **signed delta**, not the new total — see
    `ReserveMovementRequest`, which refuses zero rather than accepting a movement
    that would explain nothing.

    Returns the financials section rather than the movement that was just written.
    The caller wants to redraw the tab, and the tab needs the recomputed held
    figures, the reordered transaction list and the re-derived authority position;
    returning the single row would make the client compute all three and get one of
    them wrong.
    """
    claim = await _load(context, reference)
    await context.casework.post_movement(
        claim,
        movement_type=payload.movement_type,
        amount_minor=payload.amount_minor,
        rationale=payload.rationale,
        actor=_actor(principal),
        sub_movement_type=payload.sub_movement_type,
        currency=payload.currency,
        accounting_amount_minor=payload.accounting_amount_minor,
        accounting_currency=payload.accounting_currency,
        basis=payload.basis,
    )
    await context.commit()
    return (await context.sections.build(claim)).financials


@router.post(
    "/{reference}/coverages/{section_key}",
    response_model=sections_api.ClaimAssessmentOut,
    summary="Take a position on one coverage section",
)
async def set_coverage_standpoint(
    reference: str,
    section_key: str,
    payload: api.CoverageStandpointRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimAssessmentOut:
    """Confirm, narrow, question or exclude one section of the policy.

    CLAWS entry category 4. The sections themselves are not created here — they
    are proposed from the policy's named perils at claim creation, and
    `section_key` addresses one of them. A key the claim does not have is a 404
    rather than an implicit create: a section that is not on the contract is not a
    section a handler can take a position on.

    **A reason is required only when you depart from what the rules proposed.**
    Agreeing with a proposal is not a decision that needs justifying; overruling
    one is, and the 422 names both positions so the handler can see what they are
    disagreeing with.

    Returns the whole assessment section rather than the row, because the exposure
    figure and the over-limit list are recomputed across every section — and
    having the client re-derive them is how the header and the tab stop agreeing.
    """
    claim = await _load(context, reference)
    await context.coverage.set_standpoint(
        claim,
        section_key=section_key,
        standpoint=payload.standpoint,
        actor=_actor(principal),
        note=payload.note,
        reason=payload.reason,
        claimed_minor=payload.claimed_minor,
        sublimit_minor=payload.sublimit_minor,
    )
    await context.commit()
    return (await context.sections.build(claim)).assessment


@router.post(
    "/{reference}/parties",
    response_model=sections_api.ClaimPartyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a party to the claim",
)
async def add_claim_party(
    reference: str,
    payload: api.ClaimPartyRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimPartyOut:
    """Add somebody the notification never named. CLAWS entry category 5.

    Everything the extraction read is already on the claim, copied from the notice
    when the claim was created. This is for the three roles no broker's email
    mentions — the underwriter, the internal handler, the loss adjuster — and for
    the third party who turns up three weeks in.

    Stamped as human-entered unconditionally. A party added by hand that claimed to
    have been AI-read would be the one lie the audit trail cannot tolerate.
    """
    claim = await _load(context, reference)
    party = await context.coverage.add_party(
        claim,
        role=payload.role,
        name=payload.name,
        actor=_actor(principal),
        organisation=payload.organisation,
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
        notes=payload.notes,
    )
    await context.commit()
    return sections_api.ClaimPartyOut(
        id=party.id,
        role=str(party.role),
        name=party.name,
        organisation=party.organisation,
        email=party.email,
        phone=party.phone,
        address=party.address,
        notes=party.notes,
        is_primary=bool(party.is_primary),
        source=str(party.source),
        confidence=None,
        fnol_party_id=None,
    )


@router.post(
    "/{reference}/coverages/{section_key}/parties",
    response_model=sections_api.ClaimAssessmentOut,
    summary="Link a party to a coverage section",
)
async def link_coverage_party(
    reference: str,
    section_key: str,
    payload: api.CoveragePartyLinkRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimAssessmentOut:
    """CLAWS entry category 6 — which parties this section concerns.

    Both the section and the party are resolved **scoped to the claim**, so an id
    belonging to a different claim misses rather than linking two files together.
    A repeated link is a 409 rather than a silent success: the unique constraint
    would refuse it anyway, and a caller who thinks they linked something twice
    has a bug worth being told about.
    """
    claim = await _load(context, reference)
    await context.coverage.link_party(
        claim,
        section_key=section_key,
        party_id=payload.party_id,
        actor=_actor(principal),
        basis=payload.basis,
    )
    await context.commit()
    return (await context.sections.build(claim)).assessment


@router.delete(
    "/{reference}/coverages/{section_key}/parties/{party_id}",
    response_model=sections_api.ClaimAssessmentOut,
    summary="Unlink a party from a coverage section",
)
async def unlink_coverage_party(
    reference: str,
    section_key: str,
    party_id: uuid.UUID,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimAssessmentOut:
    """Remove a link. The party stays on the claim; only the association goes.

    A `DELETE` that returns a body, deliberately: the caller is redrawing a tab
    whose exposure figure may have moved, and a 204 would make them fetch it.
    """
    claim = await _load(context, reference)
    await context.coverage.unlink_party(
        claim, section_key=section_key, party_id=party_id, actor=_actor(principal)
    )
    await context.commit()
    return (await context.sections.build(claim)).assessment


@router.post(
    "/{reference}/deductible",
    response_model=sections_api.ClaimFinancialsOut,
    summary="Record the excess",
)
async def set_deductible(
    reference: str,
    payload: api.DeductibleRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimFinancialsOut:
    """CLAWS entry category 7 — the excess, its type, and its cap.

    `section_key` absent means the whole claim, which is the common case. Setting
    the same scope twice **replaces** rather than adding a second row: a claim has
    one excess per scope, and two would leave the arithmetic to choose.

    Returns the financials section, because an aggregate excess's remaining figure
    depends on other claims on the policy and the client cannot compute it.
    """
    claim = await _load(context, reference)
    await context.coverage.set_deductible(
        claim,
        deductible_type=payload.deductible_type,
        amount_minor=payload.amount_minor,
        actor=_actor(principal),
        section_key=payload.section_key,
        currency=payload.currency,
        maximum_applied_minor=payload.maximum_applied_minor,
        comment=payload.comment,
    )
    await context.commit()
    return (await context.sections.build(claim)).financials


# ---------------------------------------------------------------------------
# The field inspection
# ---------------------------------------------------------------------------
#
# Seven endpoints for one tab, which is more than any other section here needs,
# and the reason is that an inspection is a *process* rather than a record: it is
# instructed, booked, attended, reported on, argued with and closed, by three
# different people over several weeks. Collapsing that into one `PUT` would mean
# the audit trail could not say who booked the visit or when the report came back,
# which is most of what the trail is for.
#
# Every one of them returns the whole rebuilt section. A tab that has just booked
# a visit needs the new status, the new `next_statuses` and the new tiles, and a
# 204 would make it fetch all three.


@router.post(
    "/{reference}/inspection",
    response_model=sections_api.ClaimInspectionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Commission a field inspection",
)
async def commission_inspection(
    reference: str,
    payload: api.InspectionCommissionRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """Instruct a loss adjuster to attend the site.

    The one write that creates the record, and the answer to *"how do I schedule a
    field inspection?"* — which had no answer at all until this existed.

    An empty body is valid and is the common case: a desk knows a visit is needed
    before it knows who is doing it. Sending `scheduled_at` books it in the same
    breath, which is two acts and therefore two audit events.

    409 on a claim that already has an inspection, and on one that has been settled
    or declined.
    """
    claim = await _load(context, reference)
    actor = _actor(principal)

    await context.inspection.commission(
        claim,
        actor=actor,
        adjuster_name=payload.adjuster_name,
        adjuster_firm=payload.adjuster_firm,
        report_due_at=payload.report_due_at,
        site_kind=payload.site_kind,
        site_address=payload.site_address,
        site_identifier=payload.site_identifier,
        site_contact_name=payload.site_contact_name,
        site_contact_phone=payload.site_contact_phone,
        site_access_note=payload.site_access_note,
    )
    if payload.scheduled_at is not None:
        await context.inspection.schedule(claim, scheduled_at=payload.scheduled_at, actor=actor)

    await context.commit()
    return (await context.sections.build(claim)).inspection


@router.patch(
    "/{reference}/inspection/schedule",
    response_model=sections_api.ClaimInspectionOut,
    summary="Book the visit, or move it",
)
async def schedule_inspection(
    reference: str,
    payload: api.InspectionScheduleRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """Put a date on the visit.

    Also how a cancelled visit is rebooked, and how a second visit is arranged
    after a report was sent back — both are this call, because to a handler they are
    the same act and the trail records which one it was from the status it moved
    from.
    """
    claim = await _load(context, reference)
    await context.inspection.schedule(
        claim,
        scheduled_at=payload.scheduled_at,
        actor=_actor(principal),
        adjuster_name=payload.adjuster_name,
        adjuster_firm=payload.adjuster_firm,
        reference=payload.reference,
        report_due_at=payload.report_due_at,
    )
    await context.commit()
    return (await context.sections.build(claim)).inspection


@router.post(
    "/{reference}/inspection/attendance",
    response_model=sections_api.ClaimInspectionOut,
    summary="Record that the adjuster attended",
)
async def record_inspection_attendance(
    reference: str,
    payload: api.InspectionAttendanceRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """The visit happened. Everything the tab derives from a visit unlocks here.

    Observations cannot be logged before this, which is the point: a schedule of
    damage on a claim nobody has visited is somebody's guess, and the record should
    not be able to hold it as a finding.
    """
    claim = await _load(context, reference)
    await context.inspection.record_attendance(
        claim,
        attended_at=payload.attended_at,
        actor=_actor(principal),
        summary=payload.summary,
        photographs=payload.photographs,
        measurements=payload.measurements,
        statements=payload.statements,
        documents=payload.documents,
    )
    await context.commit()
    return (await context.sections.build(claim)).inspection


@router.patch(
    "/{reference}/inspection/status",
    response_model=sections_api.ClaimInspectionOut,
    summary="Send the report back, or accept it",
)
async def set_inspection_status(
    reference: str,
    payload: api.InspectionStatusRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """The judgements a date does not imply.

    `more_needed` sends the report back and requires a reason, because an adjuster
    told only that they were rejected cannot act on it. `completed` accepts it —
    which is not the same as *finished*: outstanding actions can and do survive an
    accepted report, and `app.domain.inspection.is_settled` insists on both.

    Anything the machine forbids is a 409 with the illegal move named, rather than
    a silently ignored write.
    """
    claim = await _load(context, reference)
    await context.inspection.set_status(
        claim, status=payload.status, actor=_actor(principal), reason=payload.reason
    )
    await context.commit()
    return (await context.sections.build(claim)).inspection


@router.post(
    "/{reference}/inspection/observations",
    response_model=sections_api.ClaimInspectionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Log what the visit found",
)
async def add_inspection_observation(
    reference: str,
    payload: api.InspectionObservationRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """One element of the risk, how badly it came off, and what it was costed at.

    The whole section comes back rather than the one row, because the quantified
    total and the priced count both move — and a client that added them up locally
    would be the second implementation of an arithmetic that has one correct answer.
    """
    claim = await _load(context, reference)
    await context.inspection.add_observation(
        claim,
        element=payload.element,
        severity=payload.severity,
        finding=payload.finding,
        actor=_actor(principal),
        quantified_minor=payload.quantified_minor,
        currency=payload.currency,
        photo_count=payload.photo_count,
    )
    await context.commit()
    return (await context.sections.build(claim)).inspection


@router.post(
    "/{reference}/inspection/actions",
    response_model=sections_api.ClaimInspectionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a follow-up on the inspection",
)
async def add_inspection_action(
    reference: str,
    payload: api.InspectionActionRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """Something that has to happen before the inspection is done with."""
    claim = await _load(context, reference)
    await context.inspection.add_action(
        claim,
        label=payload.label,
        owner=payload.owner,
        actor=_actor(principal),
        due_at=payload.due_at,
    )
    await context.commit()
    return (await context.sections.build(claim)).inspection


@router.patch(
    "/{reference}/inspection/actions/{action_id}",
    response_model=sections_api.ClaimInspectionOut,
    summary="Tick or untick an inspection action",
)
async def set_inspection_action_done(
    reference: str,
    action_id: uuid.UUID,
    payload: api.InspectionActionUpdateRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimInspectionOut:
    """Mark one follow-up done, or open again.

    The action id is looked up **within** this claim's inspection, so an id from
    another claim is a 404 rather than a cross-file write — the rule every scoped
    read here follows.
    """
    claim = await _load(context, reference)
    await context.inspection.set_action_done(
        claim, action_id=action_id, done=payload.done, actor=_actor(principal)
    )
    await context.commit()
    return (await context.sections.build(claim)).inspection


# ---------------------------------------------------------------------------
# Recoveries
# ---------------------------------------------------------------------------
#
# Money coming back. Every one of these returns the whole rebuilt section, because
# the two totals, the limitation warnings and each row's `next_statuses` all move
# together and a client deriving any of them would be the second implementation of
# an arithmetic with one correct answer.


@router.post(
    "/{reference}/recoveries",
    response_model=sections_api.ClaimRecoveriesOut,
    status_code=status.HTTP_201_CREATED,
    summary="Identify a recovery",
)
async def open_recovery(
    reference: str,
    payload: api.RecoveryOpenRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimRecoveriesOut:
    """Open a route money is expected back by.

    **Many per claim**, unlike the inspection: a fire produces a subrogation against
    a contractor, salvage on the stock and a reinsurance recovery under the treaty,
    each with its own counterparty and its own limitation date.

    Opens at `identified` — somebody has spotted a route and nobody has acted yet.
    Allowed on a decided claim, deliberately: subrogation runs for years after a
    settlement is paid, and a guard here would make the register useless on exactly
    the claims that have recoveries.
    """
    claim = await _load(context, reference)
    await context.recoveries.open(
        claim,
        kind=payload.kind,
        label=payload.label,
        actor=_actor(principal),
        expected_minor=payload.expected_minor,
        currency=payload.currency,
        prospects=payload.prospects,
        position=payload.position,
        limitation_at=payload.limitation_at,
        party_name=payload.party_name,
        party_role=payload.party_role,
        party_carrier=payload.party_carrier,
        party_carrier_reference=payload.party_carrier_reference,
        party_contact=payload.party_contact,
    )
    await context.commit()
    return (await context.sections.build(claim)).recoveries


@router.patch(
    "/{reference}/recoveries/{recovery_id}",
    response_model=sections_api.ClaimRecoveriesOut,
    summary="Move a recovery on, or bank what came back",
)
async def progress_recovery(
    reference: str,
    recovery_id: uuid.UUID,
    payload: api.RecoveryProgressRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimRecoveriesOut:
    """One endpoint for the status, the money and the position.

    To a handler these are one act: they have heard something and they are writing
    down what it means. Each call appends an event to the recovery's own history, so
    the tab reads as a narrative rather than a set of current values.

    `recovered_minor` is a **running total**. A lower figure than the one already
    recorded is refused: that is a correction, and a correction to a money column
    belongs in a ledger rather than an overwrite.
    """
    claim = await _load(context, reference)
    await context.recoveries.progress(
        claim,
        recovery_id=recovery_id,
        actor=_actor(principal),
        status=payload.status,
        note=payload.note,
        expected_minor=payload.expected_minor,
        recovered_minor=payload.recovered_minor,
        prospects=payload.prospects,
        position=payload.position,
        limitation_at=payload.limitation_at,
    )
    await context.commit()
    return (await context.sections.build(claim)).recoveries


@router.post(
    "/{reference}/recoveries/tasks",
    response_model=sections_api.ClaimRecoveriesOut,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a recovery task",
)
async def add_recovery_task(
    reference: str,
    payload: api.RecoveryTaskRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimRecoveriesOut:
    """Something that has to happen for a recovery to progress.

    `recovery_id` is optional: recovery work exists before anybody knows which
    recovery it will support.
    """
    claim = await _load(context, reference)
    await context.recoveries.add_task(
        claim,
        label=payload.label,
        owner=payload.owner,
        actor=_actor(principal),
        recovery_id=payload.recovery_id,
        due_at=payload.due_at,
    )
    await context.commit()
    return (await context.sections.build(claim)).recoveries


@router.patch(
    "/{reference}/recoveries/tasks/{task_id}",
    response_model=sections_api.ClaimRecoveriesOut,
    summary="Tick or untick a recovery task",
)
async def set_recovery_task_done(
    reference: str,
    task_id: uuid.UUID,
    payload: api.RecoveryTaskUpdateRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimRecoveriesOut:
    """Mark one task done, or open again."""
    claim = await _load(context, reference)
    await context.recoveries.set_task_done(
        claim, task_id=task_id, done=payload.done, actor=_actor(principal)
    )
    await context.commit()
    return (await context.sections.build(claim)).recoveries


# ---------------------------------------------------------------------------
# Fraud and the SIU case
# ---------------------------------------------------------------------------


@router.post(
    "/{reference}/siu/referral",
    response_model=sections_api.ClaimFraudReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Refer the claim to special investigations",
)
async def refer_to_siu(
    reference: str,
    payload: api.SiuReferralRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimFraudReviewOut:
    """Hand the claim to SIU, with a recorded reason.

    **This is what `siu_status` used to be derived from a flag for.** The tab read
    `claims.fraud_flag` and reported `screening`, which asserted that somebody was
    looking when nothing but the model had. A case exists because a person opened
    one, and only a person moves it.

    The reason is required. A referral leaves the claims desk and is an accusation of
    sorts against the insured.
    """
    claim = await _load(context, reference)
    await context.siu.refer(
        claim,
        actor=_actor(principal),
        reason=payload.reason,
        investigator=payload.investigator,
        siu_reference=payload.siu_reference,
    )
    await context.commit()
    return (await context.sections.build(claim)).fraud


@router.post(
    "/{reference}/siu/screening",
    response_model=sections_api.ClaimFraudReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record that the claim is being screened",
)
async def screen_for_fraud(
    reference: str,
    payload: api.SiuScreenRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimFraudReviewOut:
    """Somebody is looking, and nobody is being accused yet.

    The state the tab could never reach honestly, and worth having: it is the
    difference between the model having scored a claim and a person examining it.
    """
    claim = await _load(context, reference)
    await context.siu.screen(claim, actor=_actor(principal), note=payload.note)
    await context.commit()
    return (await context.sections.build(claim)).fraud


@router.patch(
    "/{reference}/siu",
    response_model=sections_api.ClaimFraudReviewOut,
    summary="Move the investigation",
)
async def set_siu_status(
    reference: str,
    payload: api.SiuStatusRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimFraudReviewOut:
    """Take the case through the investigation, and close it.

    Closing requires an outcome and investigating requires an investigator: an
    investigation nobody owns is not one, and a case closed with nothing recorded
    cannot answer the only question anybody asks of it later.

    A closed case can be reopened. New information arrives, and a second case for the
    same suspicion would split one story in half.
    """
    claim = await _load(context, reference)
    await context.siu.set_status(
        claim,
        status=payload.status,
        actor=_actor(principal),
        investigator=payload.investigator,
        outcome=payload.outcome,
        siu_reference=payload.siu_reference,
        recommended_actions=payload.recommended_actions,
    )
    await context.commit()
    return (await context.sections.build(claim)).fraud


@router.post(
    "/{reference}/fraud/dispositions",
    response_model=sections_api.ClaimFraudReviewOut,
    summary="Accept or discount a fraud indicator",
)
async def dispose_fraud_indicator(
    reference: str,
    payload: api.FraudDispositionRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> sections_api.ClaimFraudReviewOut:
    """Record a verdict on one indicator, durably.

    **This is the write the tab used to admit it did not have.** Verdicts were held
    in a working copy on the client, so a handler who worked through eight indicators
    lost all eight by reloading.

    Addressed by the indicator's own `code` rather than a row id, because the
    indicators are derived from the stored fraud analysis and have no rows. Changing
    a verdict updates the one row the unique constraint allows; the audit trail keeps
    what it used to say.

    `discounted` requires a note. Accepting an indicator agrees with the assessment;
    dismissing one is a person overruling a fraud signal.
    """
    claim = await _load(context, reference)
    await context.siu.dispose(
        claim,
        code=payload.code,
        disposition=payload.disposition,
        actor=_actor(principal),
        note=payload.note,
    )
    await context.commit()
    return (await context.sections.build(claim)).fraud


@router.post(
    "/{reference}/decision",
    response_model=api.ClaimDetail,
    summary="Take a decision on the claim",
)
async def decide(
    reference: str,
    payload: api.ClaimDecisionRequest,
    context: FNOLContextDep,
    principal: WorkAccess,
) -> api.ClaimDetail:
    """Approve, refer, request information or decline.

    The five verbs and the statuses they land on are in
    `app.domain.claim_lifecycle`; what blocks a settlement is `approval_blocks`,
    which is the same function the workbench renders above the decision bar. A
    handler is therefore never refused for a reason the screen did not already show
    them — and a blocked attempt is audited rather than silently rejected.

    Returns the claim detail, so the queue's preview pane and the workbench header
    both redraw from one response.
    """
    claim = await _load(context, reference)
    await context.casework.decide(
        claim, decision=payload.decision, actor=_actor(principal), reason=payload.reason
    )
    await context.commit()
    return await _detail(context, claim)


@router.get(
    "/{reference}/knowledge-graph",
    response_model=kg_api.ClaimKnowledgeGraph,
    summary="The claim's facts as a connected graph",
)
async def get_knowledge_graph(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> kg_api.ClaimKnowledgeGraph:
    """The claim, the contract, the incident, the evidence and the risk, joined.

    The same records the workbench reads, stated as entities and relationships
    instead of as panels. One call for the same reason the workbench is one call:
    a graph assembled from six endpoints could show an asset joined to a coverage
    check that was computed against a different policy read.

    The route gathers and delegates; the join itself is in
    `app.domain.knowledge_graph`, which is pure and has no session.
    """
    del principal
    claim = await _load(context, reference)
    graph_input = await _graph_input(context, claim)
    return kg_api.to_knowledge_graph(build_claim_graph(graph_input))


async def _graph_input(context: FNOLContext, claim: Claim) -> GraphInput:
    """Gather everything the graph builder reads.

    The two lookups that are *not* simply "load the claim's rows" are deliberate:
    catastrophe candidates are fetched even when nothing was attributed, because a
    rejected attribution is a finding a handler wants offered; and other claims on
    the same policy are fetched because loss history is the pattern no single
    claim screen can show.
    """
    case = await context.cases.get(claim.fnol_case_id) if claim.fnol_case_id else None

    analyses: dict[str, Any] = {}
    documents: list[Any] = []
    fields: list[Any] = []
    exceptions: list[Any] = []
    parties: list[Any] = []
    duplicates: list[Any] = []
    policy_matches: list[Any] = []

    if case is not None:
        documents = list(await context.cases.list_documents(case.id))
        fields = list(await context.cases.list_fields(case.id))
        exceptions = list(await context.cases.list_exceptions(case.id))
        parties = list(await context.cases.list_parties(case.id))
        duplicates = list(await context.cases.list_duplicates(case.id))
        policy_matches = list(await context.cases.list_policy_matches(case.id))
        analyses = {row.kind: row for row in await context.cases.list_analyses(case.id)}

    policy_id = claim.policy_id or (case.policy_id if case else None)
    policy = await context.policies.get(policy_id) if policy_id else None

    cat_event_id = claim.cat_event_id or (case.cat_event_id if case else None)
    cat_event = await context.cat_events.get(cat_event_id) if cat_event_id else None
    cat_candidates: list[Any] = []
    if cat_event is None and claim.date_of_loss is not None:
        cat_candidates = list(
            await context.cat_events.find_in_window(
                claim.date_of_loss.date(), country=claim.loss_country
            )
        )

    related: list[Any] = []
    if claim.policy_number:
        rows, _ = await context.claims.list_queue(search=claim.policy_number, limit=6)
        related = [other for other in rows if other.id != claim.id]

    return GraphInput(
        claim=claim,
        case=case,
        policy=policy,
        documents=documents,
        fields=fields,
        exceptions=exceptions,
        parties=parties,
        duplicates=duplicates,
        policy_matches=policy_matches,
        analyses=analyses,
        cat_event=cat_event,
        cat_candidates=cat_candidates,
        related_claims=related,
        assignment=await context.claims.get_assignment(claim.id),
        triage=await context.claims.get_triage(claim.id),
        include_seeded=settings.knowledge_graph_seeded_entities,
    )


@router.get(
    "/{reference}/audit",
    response_model=list[Any],
    summary="The claim's audit trail, including its notification's",
)
async def claim_audit(reference: str, context: FNOLContextDep, principal: ReadAccess) -> list[Any]:
    del principal
    claim = await _load(context, reference)
    related = (claim.fnol_case_id,) if claim.fnol_case_id else ()
    events = await context.audit.history(claim.id, related_ids=related)
    return [to_audit_event(event) for event in events]


@router.post(
    "/{reference}/assignment",
    response_model=api.ClaimDetail,
    summary="Assign the claim, or accept the recommendation",
)
async def assign(
    reference: str,
    payload: api.AssignmentRequest,
    context: FNOLContextDep,
    principal: AssignAccess,
) -> api.ClaimDetail:
    claim = await _load(context, reference)
    assignment = await context.assignment.assign(
        claim, handler_id=payload.handler_id, actor=_actor(principal)
    )
    if assignment is None:
        raise ValidationError(
            "There is no handler to assign. Choose one explicitly, or leave the claim "
            "on its queue for a manager to allocate."
        )

    context.audit.claim(
        claim,
        event_type=AuditEventType.HANDLER_ASSIGNED,
        summary=f"{assignment.handler_name} assigned by {_actor(principal)}.",
        actor=_actor(principal),
        after={"handler": assignment.handler_name, "team": assignment.team},
        context={"reason": payload.reason} if payload.reason else {},
    )
    await context.commit()
    return await _detail(context, claim)


@router.post(
    "/{reference}/triage",
    response_model=api.ClaimDetail,
    summary="Override the triage route",
)
async def override_triage(
    reference: str,
    payload: api.TriageOverrideRequest,
    context: FNOLContextDep,
    principal: AssignAccess,
) -> api.ClaimDetail:
    claim = await _load(context, reference)
    triage_row = await context.triage.override(
        claim,
        route_key=payload.route_key,
        route_label=payload.route_label,
        priority=payload.priority,
        reason=payload.reason,
        actor=_actor(principal),
    )
    if triage_row is None:
        raise NotFoundError("This claim has not been triaged yet.")

    context.audit.claim(
        claim,
        event_type=AuditEventType.TRIAGE_OVERRIDDEN,
        summary=f"Route changed to {payload.route_label} by {_actor(principal)}.",
        actor=_actor(principal),
        before={"route": triage_row.original_route},
        after={"route": payload.route_label, "priority": payload.priority},
        context={"reason": payload.reason},
    )
    await context.commit()
    return await _detail(context, claim)


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


async def _load(context: FNOLContext, reference: str) -> Claim:
    claim = await context.claims.get_by_reference(reference)
    if claim is None:
        raise NotFoundError(f"No claim found for {reference}.")
    return claim


def _actor(principal: Principal) -> str:
    return principal.full_name or principal.username or principal.email or principal.subject


def _age_days(claim: Claim) -> int | None:
    if claim.status in (ClaimStatus.APPROVED, ClaimStatus.REJECTED):
        return None
    return max(0, (datetime.now(UTC) - claim.reported_at).days)


def _summary(claim: Claim) -> api.ClaimSummary:
    return api.ClaimSummary(
        id=claim.id,
        reference=claim.reference,
        claimant=claim.claimant_name or claim.insured_name or "—",
        location=claim.loss_location or claim.loss_country or "—",
        loss_type=(claim.loss_type or claim.line_of_business or "—").replace("_", " ").title(),
        status=claim.status,
        priority=claim.priority,
        reported_at=claim.reported_at,
        age_days=_age_days(claim),
        reserve=Money(amount_minor=claim.reserve_minor or 0, currency=claim.currency),
        over_authority=bool(claim.over_authority),
        fraud_flag=bool(claim.fraud_flag),
        cat_linked=claim.cat_event_id is not None,
        handler_name=claim.handler_name,
        severity=claim.severity,
        fnol_reference=None,
    )


async def _detail(context: FNOLContext, claim: Claim) -> api.ClaimDetail:
    case = await context.cases.get(claim.fnol_case_id) if claim.fnol_case_id else None
    policy = await context.policies.get(claim.policy_id) if claim.policy_id else None
    triage_row = await context.claims.get_triage(claim.id)
    assignment_row = await context.claims.get_assignment(claim.id)
    cat_event = await context.cat_events.get(claim.cat_event_id) if claim.cat_event_id else None

    handler = (
        await context.handlers.get(assignment_row.handler_id)
        if assignment_row and assignment_row.handler_id
        else None
    )
    authority = (
        Money(amount_minor=handler.authority_limit_minor, currency=handler.currency)
        if handler and handler.authority_limit_minor
        else None
    )
    # Subtracted in one currency or not at all. The authority limit is the
    # handler's and the reserve is the claim's, and "£500,000 authority, $600,000
    # reserve" is not a £100,000 overrun.
    authority_in_claim_currency = (
        to_currency(
            authority.amount_minor, authority.currency, claim.currency, config=settings.fnol
        )
        if authority
        else None
    )
    over_by = (
        Money(
            amount_minor=claim.reserve_minor - authority_in_claim_currency.amount_minor,
            currency=claim.currency,
        )
        if authority_in_claim_currency
        and claim.reserve_minor > authority_in_claim_currency.amount_minor
        else None
    )

    documents = await context.cases.list_documents(case.id) if case else []
    notes = await context.cases.list_notes(case.id) if case else []
    events = await context.audit.history(claim.id, related_ids=(case.id,) if case else ())

    return api.ClaimDetail(
        id=claim.id,
        reference=claim.reference,
        status=claim.status,
        priority=claim.priority,
        severity=claim.severity,
        claimant_name=claim.claimant_name,
        insured_name=claim.insured_name,
        policy_number=claim.policy_number,
        policy_period=(
            f"{policy.effective_date:%d %b %Y} – {policy.expiry_date:%d %b %Y}" if policy else None
        ),
        line_of_business=claim.line_of_business,
        loss_type=claim.loss_type,
        loss_description=claim.loss_description,
        loss_location=claim.loss_location,
        loss_country=claim.loss_country,
        date_of_loss=claim.date_of_loss,
        reported_at=claim.reported_at,
        age_days=_age_days(claim),
        reserve=Money(amount_minor=claim.reserve_minor or 0, currency=claim.currency),
        paid=Money(amount_minor=claim.paid_minor or 0, currency=claim.currency),
        authority_limit=authority,
        over_authority_by=over_by,
        over_authority=bool(claim.over_authority),
        fraud_flag=bool(claim.fraud_flag),
        cat_reference=cat_event.reference if cat_event else None,
        handler_name=claim.handler_name,
        created_by=claim.created_by,
        fnol_reference=case.reference if case else None,
        fnol_summary=case.ai_summary if case else None,
        completeness_score=float(case.completeness_score)
        if case and case.completeness_score is not None
        else None,
        triage=to_triage(triage_row) if triage_row else None,
        assignment=to_assignment(assignment_row) if assignment_row else None,
        coverage=await _coverage(context, case),
        fraud=await _fraud(context, case),
        documents=[to_document(document) for document in documents],
        notes=[to_note(note) for note in notes],
        activity=[
            api.ActivityEntryOut(
                id=event.id,
                description=event.summary,
                actor=event.actor,
                actor_type=event.actor_type,
                occurred_at=event.occurred_at,
                kind=_activity_kind(event.event_type, event.actor_type),
            )
            for event in events
        ],
    )


async def _coverage(context: FNOLContext, case: Any) -> api.CoverageReviewOut | None:
    """The preliminary coverage read, as it was computed at intake.

    Read from the stored analysis rather than recomputed: the claim's file should
    show what was concluded when the claim was created, not what today's policy
    data would conclude.
    """
    if case is None:
        return None
    analysis = await context.cases.get_analysis(case.id, AnalysisKind.COVERAGE)
    if analysis is None:
        return None

    result = analysis.result or {}
    return api.CoverageReviewOut(
        indicator=result.get("indicator", "insufficient_information"),
        confidence=float(analysis.confidence) if analysis.confidence is not None else None,
        reasoning=result.get("reasoning", ""),
        checks=[
            api.CoverageCheckOut(
                key=check.get("key", ""),
                label=check.get("label", ""),
                state=check.get("state", "unknown"),
                detail=check.get("detail", ""),
            )
            for check in result.get("checks", [])
        ],
        completed_at=analysis.updated_at,
        produced_by=analysis.provider,
    )


async def _fraud(context: FNOLContext, case: Any) -> api.FraudAssessmentOut | None:
    if case is None:
        return None
    analysis = await context.cases.get_analysis(case.id, AnalysisKind.FRAUD)
    if analysis is None:
        return None

    result = analysis.result or {}
    return api.FraudAssessmentOut(
        level=result.get("level", "low"),
        score=float(result.get("score", 0.0)),
        indicators=[
            api.FraudIndicatorOut(
                code=indicator.get("code", ""),
                title=indicator.get("title", ""),
                detail=indicator.get("detail", ""),
                weight=float(indicator.get("weight", 0.0)),
            )
            for indicator in result.get("indicators", [])
        ],
    )


def _activity_kind(event_type: str, actor_type: str) -> str:
    if event_type.endswith(("document_uploaded", "document_deleted")):
        return "evidence"
    if actor_type == "ai":
        return "assistant"
    return "decision"


def _stage_note(claim: Claim, assignment: Any) -> str:
    if assignment is None:
        return "This claim has not been triaged."
    if assignment.status == "assigned":
        return f"With {assignment.handler_name} on the {assignment.team} desk."
    if assignment.handler_name:
        return (
            f"Recommended to {assignment.handler_name} ({assignment.team}); "
            "awaiting a manager's acceptance."
        )
    return f"Queued to {assignment.queue} for allocation."


def _compact(amount_minor: int, currency: str) -> str:
    """`£4.82m`, `£128k`, `£940`. The abbreviation is a server concern.

    The queue has to be able to compare these down a column, and a client that
    rounded them itself would eventually round them differently from the tile
    above the column.
    """
    units = to_major(amount_minor, currency)
    symbol = {"GBP": "£", "USD": "$", "EUR": "€", "SGD": "S$"}.get(currency, "")
    if abs(units) >= 1_000_000:
        return f"{symbol}{_trim(units / 1_000_000, 2)}m"
    if abs(units) >= 1_000:
        return f"{symbol}{_trim(units / 1_000, 1)}k"
    return f"{symbol}{units:,.0f}"


def _trim(value: float, places: int) -> str:
    """`4.82`, `128`, `1.5` — a trailing zero decimal is noise in a tile."""
    return f"{value:,.{places}f}".rstrip("0").rstrip(".")
