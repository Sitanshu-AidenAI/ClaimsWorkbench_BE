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

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps.auth import require_roles
from app.api.deps.services import FNOLContext, FNOLContextDep
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import (
    CLAIM_ASSIGN_ROLES,
    FNOL_READ_ROLES,
    AnalysisKind,
    AuditEventType,
    ClaimStatus,
)
from app.domain.knowledge_graph import GraphInput, build_claim_graph
from app.domain.money import to_currency, to_major
from app.models.claim import Claim
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
    """
    actor = _actor(principal)
    statuses = [status_filter] if status_filter and status_filter != "all" else None

    rows, total = await context.claims.list_queue(
        statuses=statuses,
        handler_name=actor if scope == "assigned" else None,
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

    blocks: list[str] = []
    if claim.fraud_flag:
        blocks.append("clear the fraud review flag")
    if claim.over_authority:
        blocks.append("refer the settlement — it is above your authority")
    if assignment_row is None or assignment_row.status != "assigned":
        blocks.append("assign a handler")

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
        approval_blocks=blocks,
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
