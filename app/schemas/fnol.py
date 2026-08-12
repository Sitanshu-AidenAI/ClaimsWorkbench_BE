"""Request and response shapes for the FNOL API.

Response models are built by explicit mappers at the bottom of this module rather
than by `from_attributes` over the ORM rows. That costs a few lines and buys the
thing that matters on a claims API: adding a column to a table cannot
accidentally publish it. Everything the client sees is listed here.

Money is always `{amount_minor, currency}` — the same shape the frontend's `Money`
type already uses — because a float is not a thing to hold a reserve in.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.domain.enums import (
    DuplicateResolution,
    ExceptionStatus,
    FNOLChannel,
    FNOLStatus,
    Severity,
)
from app.schemas.common import SchemaBase

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class Money(SchemaBase):
    amount_minor: int
    currency: str


def money(amount_minor: int | None, currency: str | None) -> Money | None:
    if amount_minor is None:
        return None
    return Money(amount_minor=amount_minor, currency=currency or "GBP")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class NotificationCreate(SchemaBase):
    """A notification arriving through a structured channel."""

    channel: FNOLChannel
    body: str = Field(min_length=1, max_length=100_000)
    received_at: datetime | None = None

    reporter_name: str | None = Field(default=None, max_length=255)
    reporter_organisation: str | None = Field(default=None, max_length=255)
    reporter_role: str | None = Field(default=None, max_length=96)
    reporter_email: str | None = Field(default=None, max_length=255)
    reporter_phone: str | None = Field(default=None, max_length=64)

    external_reference: str | None = Field(default=None, max_length=128)
    source_reference: str | None = Field(default=None, max_length=128)

    #: Values the channel states outright rather than in prose. Recorded with
    #: `channel` provenance so the officer can see nothing interpreted them.
    supplied: dict[str, Any] = Field(default_factory=dict)
    #: Free-form record of the submission — a portal payload, a call script.
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    #: Run the pipeline as part of the create call. False leaves it queued.
    process: bool = True


class EmailAttachmentIn(SchemaBase):
    filename: str = Field(max_length=255)
    #: Base64. Attachments arrive as bytes from a mailbox, not as multipart.
    content_base64: str
    content_type: str | None = None


class EmailNotificationCreate(SchemaBase):
    """An inbound claim email, as a mailbox integration would deliver it."""

    sender: str = Field(max_length=320)
    recipient: str = Field(max_length=320)
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=200_000)
    message_id: str = Field(max_length=512)
    received_at: datetime | None = None
    thread_id: str | None = Field(default=None, max_length=512)
    cc: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[EmailAttachmentIn] = Field(default_factory=list)
    from_broker: bool = True
    body_is_html: bool = False
    process: bool = True


# ---------------------------------------------------------------------------
# Case reads
# ---------------------------------------------------------------------------


class FNOLSummary(SchemaBase):
    """A row in the intake queue."""

    id: uuid.UUID
    reference: str
    title: str
    status: FNOLStatus
    channel: FNOLChannel
    processing_state: str
    received_at: datetime
    date_of_loss: datetime | None
    insured_name: str | None
    reporter_name: str | None
    policy_number: str | None
    line_of_business: str | None
    loss_type: str | None
    loss_location: str | None
    severity: Severity | None
    estimated_loss: Money | None
    completeness_score: float | None
    fraud_risk: str | None
    coverage_indicator: str | None
    open_exceptions: int
    blocking_exceptions: int
    assigned_to: str | None
    claim_reference: str | None


class FNOLDocumentOut(SchemaBase):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    kind: str
    source: str
    extraction_status: str
    text_characters: int
    page_count: int | None
    extraction_error: str | None
    uploaded_by: str | None
    created_at: datetime


class FNOLFieldOut(SchemaBase):
    """One value and its provenance — the review screen's core row."""

    path: str
    section: str
    label: str
    value: str | None
    confidence: float | None
    source: str
    human_modified: bool
    original_value: str | None
    modified_by: str | None
    modified_at: datetime | None
    override_reason: str | None
    evidence: str | None
    source_document_id: uuid.UUID | None


class FNOLPartyOut(SchemaBase):
    id: uuid.UUID
    role: str
    name: str
    organisation: str | None
    email: str | None
    phone: str | None
    address: str | None
    is_primary: bool
    source: str
    confidence: float | None


class PolicyCandidateOut(SchemaBase):
    policy_id: uuid.UUID
    policy_number: str
    insured_name: str
    line_of_business: str
    status: str
    effective_date: str
    expiry_date: str
    primary_location: str | None
    limit: Money | None
    deductible: Money | None
    match_strength: str
    score: float
    matched_on: dict[str, float]
    reasoning: str | None
    selected: bool
    selected_by: str | None


class DuplicateReasonOut(SchemaBase):
    signal: str
    detail: str
    score: float


class DuplicateCandidateOut(SchemaBase):
    id: uuid.UUID
    kind: str
    reference: str
    score: float
    reasons: list[DuplicateReasonOut]
    resolution: DuplicateResolution
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_note: str | None


class ExceptionOut(SchemaBase):
    id: uuid.UUID
    code: str
    severity: str
    title: str
    detail: str | None
    blocking: bool
    status: ExceptionStatus
    context: dict[str, Any]
    resolution_note: str | None
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime


class NoteOut(SchemaBase):
    id: uuid.UUID
    author: str
    body: str
    created_at: datetime


class AnalysisOut(SchemaBase):
    """What one analysis concluded, and who concluded it."""

    kind: str
    provider: str
    model: str | None
    confidence: float | None
    status: str
    error: str | None
    result: dict[str, Any]
    updated_at: datetime


class SourceOut(SchemaBase):
    channel: FNOLChannel
    received_at: datetime
    message_id: str | None
    thread_id: str | None
    external_reference: str | None
    source_reference: str | None
    metadata: dict[str, Any]
    body: str | None


class TriageOut(SchemaBase):
    categories: list[str]
    route: str
    route_key: str
    priority: str
    required_skill: str | None
    required_team: str | None
    confidence: float | None
    reasoning: str | None
    factors: list[dict[str, Any]]
    overridden: bool
    overridden_by: str | None
    override_reason: str | None
    original_route: str | None


class AssignmentOut(SchemaBase):
    handler_id: uuid.UUID | None
    handler_name: str | None
    team: str | None
    queue: str | None
    status: str
    strategy: str | None
    confidence: float | None
    reasoning: str | None
    alternatives: list[dict[str, Any]]
    assigned_by: str | None
    assigned_at: datetime | None


class BlockerOut(SchemaBase):
    code: str
    message: str


class CatEventOut(SchemaBase):
    id: uuid.UUID
    reference: str
    name: str
    event_type: str
    severity: str | None
    start_date: str
    end_date: str
    country: str | None
    region: str | None
    confidence: float | None
    confirmed: bool


class FNOLDetail(SchemaBase):
    """Everything the review workspace reads, in one call."""

    id: uuid.UUID
    reference: str
    title: str
    status: FNOLStatus
    processing_state: str
    processing_error: str | None
    processing_completed_at: datetime | None

    source: SourceOut

    reporter_name: str | None
    reporter_organisation: str | None
    reporter_role: str | None
    reporter_email: str | None
    reporter_phone: str | None

    policy_number: str | None
    insured_name: str | None
    insured_organisation: str | None
    policy_type: str | None
    policy_id: uuid.UUID | None
    policy_confirmed: bool

    line_of_business: str | None
    claim_type: str | None
    loss_type: str | None
    complexity: str | None
    classification_confidence: float | None
    classification_overridden: bool

    date_of_loss: datetime | None
    loss_location: str | None
    loss_country: str | None
    loss_description: str | None
    cause_of_loss: str | None
    affected_assets: str | None
    injuries: int | None
    fatalities: int | None
    business_interruption: bool
    structural_damage: bool
    environmental_exposure: bool

    estimated_loss: Money | None
    repair_estimate: Money | None
    currency: str

    police_reference: str | None
    incident_reference: str | None
    authorities_involved: str | None
    potential_litigation: bool

    severity: Severity | None
    severity_confidence: float | None
    severity_overridden: bool
    fraud_risk: str | None
    fraud_score: float | None
    coverage_indicator: str | None
    completeness_score: float | None
    extraction_confidence: float | None

    ai_summary: str | None
    ai_summary_generated_at: datetime | None

    cat_event: CatEventOut | None
    claim_reference: str | None
    assigned_to: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    documents: list[FNOLDocumentOut]
    fields: list[FNOLFieldOut]
    parties: list[FNOLPartyOut]
    policy_candidates: list[PolicyCandidateOut]
    duplicates: list[DuplicateCandidateOut]
    exceptions: list[ExceptionOut]
    analyses: dict[str, AnalysisOut]
    notes: list[NoteOut]
    blockers: list[BlockerOut]
    can_create_claim: bool


class FNOLListResult(SchemaBase):
    items: list[FNOLSummary]
    total: int
    page: int
    page_size: int


class BoardMetric(SchemaBase):
    id: str
    label: str
    value: int
    note: str


class BoardInsight(SchemaBase):
    id: str
    count: int
    label: str


class IngestStageOut(SchemaBase):
    id: str
    label: str
    done: int
    total: int


class BoardResult(SchemaBase):
    """One call for the intake command centre: figures, queue and insights."""

    items: list[FNOLSummary]
    total: int
    metrics: list[BoardMetric]
    insights: list[BoardInsight]
    ingest: list[IngestStageOut]
    status_counts: dict[str, int]
    channel_counts: dict[str, int]
    captured_at: datetime


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class FieldUpdateRequest(SchemaBase):
    """An officer's corrections. Keys are field paths, values are what they typed."""

    updates: dict[str, Any] = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=1000)
    #: Re-run the pipeline once the edits land. Default true, because a corrected
    #: date of loss changes the policy match, the CAT match and the exceptions.
    reprocess: bool = True


class PolicySelectionRequest(SchemaBase):
    policy_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=1000)


class DuplicateResolutionRequest(SchemaBase):
    resolution: Literal["new_claim", "linked", "duplicate"]
    note: str | None = Field(default=None, max_length=1000)


class ExceptionResolutionRequest(SchemaBase):
    status: Literal["resolved", "dismissed"] = "resolved"
    note: str | None = Field(default=None, max_length=1000)


class SeverityOverrideRequest(SchemaBase):
    severity: Severity
    reason: str = Field(min_length=3, max_length=1000)


class ClassificationOverrideRequest(SchemaBase):
    line_of_business: str = Field(max_length=48)
    loss_type: str | None = Field(default=None, max_length=64)
    claim_type: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=3, max_length=1000)


class CatMatchRequest(SchemaBase):
    confirmed: bool
    event_id: uuid.UUID | None = None


class NoteRequest(SchemaBase):
    body: str = Field(min_length=1, max_length=4000)


class TransitionRequest(SchemaBase):
    status: FNOLStatus
    reason: str | None = Field(default=None, max_length=1000)


class ClaimCreationRequest(SchemaBase):
    #: Echoed into the audit trail. The database constraint is what actually
    #: prevents a second claim; this is for tracing a retry to its origin.
    idempotency_key: str | None = Field(default=None, max_length=128)


class ProcessRequest(SchemaBase):
    #: Re-read the notification even when nothing it depends on has changed.
    force: bool = False


class ProcessResult(SchemaBase):
    reference: str
    status: FNOLStatus
    processing_state: str
    extraction_reused: bool
    exceptions_raised: int
    error: str | None


class AuditEventOut(SchemaBase):
    id: uuid.UUID
    entity_type: str
    entity_reference: str | None
    event_type: str
    summary: str
    actor: str
    actor_type: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    context: dict[str, Any]
    occurred_at: datetime


class PolicySearchResult(SchemaBase):
    items: list[PolicyCandidateOut]


PageSize = Annotated[int, Field(ge=1, le=100)]


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def case_title(case: Any) -> str:
    """A one-line title for the queue: what happened, and where.

    Built from the notice rather than stored, because every part of it is a field
    an officer can correct — a stored title would drift from the case the moment
    one of them did.
    """
    what = (case.loss_type or "").replace("_", " ").strip().title()
    if not what and case.loss_description:
        first = case.loss_description.strip().split(".")[0]
        what = first[:60].strip()
    if not what:
        what = (case.line_of_business or "Notification").replace("_", " ").title()

    where = (case.loss_location or case.loss_country or "").split(",")[-1].strip()
    return f"{what} — {where}" if where else what


def to_summary(
    case: Any,
    *,
    open_exceptions: int,
    blocking_exceptions: int,
    claim_reference: str | None,
) -> FNOLSummary:
    return FNOLSummary(
        id=case.id,
        reference=case.reference,
        title=case_title(case),
        status=FNOLStatus(case.status),
        channel=FNOLChannel(case.channel),
        processing_state=case.processing_state,
        received_at=case.received_at,
        date_of_loss=case.date_of_loss,
        insured_name=case.insured_name,
        reporter_name=case.reporter_name,
        policy_number=case.policy_number,
        line_of_business=case.line_of_business,
        loss_type=case.loss_type,
        loss_location=case.loss_location,
        severity=Severity(case.severity) if case.severity else None,
        estimated_loss=money(case.estimated_loss_minor, case.currency),
        completeness_score=_as_float(case.completeness_score),
        fraud_risk=case.fraud_risk,
        coverage_indicator=case.coverage_indicator,
        open_exceptions=open_exceptions,
        blocking_exceptions=blocking_exceptions,
        assigned_to=case.assigned_to,
        claim_reference=claim_reference,
    )


def to_document(document: Any) -> FNOLDocumentOut:
    return FNOLDocumentOut(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        kind=document.document_kind,
        source=document.source,
        extraction_status=document.extraction_status,
        text_characters=document.text_characters or 0,
        page_count=document.page_count,
        extraction_error=document.extraction_error,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )


def to_field(field: Any) -> FNOLFieldOut:
    return FNOLFieldOut(
        path=field.field_path,
        section=field.section,
        label=field.label,
        value=field.value_text,
        confidence=_as_float(field.confidence),
        source=field.source,
        human_modified=bool(field.human_modified),
        original_value=field.original_value,
        modified_by=field.modified_by,
        modified_at=field.modified_at,
        override_reason=field.override_reason,
        evidence=field.evidence_snippet,
        source_document_id=field.source_document_id,
    )


def to_party(party: Any) -> FNOLPartyOut:
    return FNOLPartyOut(
        id=party.id,
        role=party.role,
        name=party.name,
        organisation=party.organisation,
        email=party.email,
        phone=party.phone,
        address=party.address,
        is_primary=bool(party.is_primary),
        source=party.source,
        confidence=_as_float(party.confidence),
    )


def to_policy_candidate(match: Any, policy: Any) -> PolicyCandidateOut:
    return PolicyCandidateOut(
        policy_id=policy.id,
        policy_number=policy.policy_number,
        insured_name=policy.insured_name,
        line_of_business=policy.line_of_business,
        status=policy.status,
        effective_date=policy.effective_date.isoformat(),
        expiry_date=policy.expiry_date.isoformat(),
        primary_location=policy.primary_location,
        limit=money(policy.limit_amount_minor, policy.currency),
        deductible=money(policy.deductible_amount_minor, policy.currency),
        match_strength=match.match_strength if match else "none",
        score=_as_float(match.score) or 0.0 if match else 0.0,
        matched_on={key: float(value) for key, value in (match.matched_on or {}).items()}
        if match
        else {},
        reasoning=match.reasoning if match else None,
        selected=bool(match.selected) if match else False,
        selected_by=match.selected_by if match else None,
    )


def to_duplicate(candidate: Any) -> DuplicateCandidateOut:
    return DuplicateCandidateOut(
        id=candidate.id,
        kind=candidate.candidate_kind,
        reference=candidate.candidate_reference,
        score=_as_float(candidate.score) or 0.0,
        reasons=[
            DuplicateReasonOut(
                signal=reason.get("signal", ""),
                detail=reason.get("detail", ""),
                score=float(reason.get("score", 0.0)),
            )
            for reason in (candidate.reasons or [])
        ],
        resolution=DuplicateResolution(candidate.resolution),
        resolved_by=candidate.resolved_by,
        resolved_at=candidate.resolved_at,
        resolution_note=candidate.resolution_note,
    )


def to_exception(exception: Any) -> ExceptionOut:
    return ExceptionOut(
        id=exception.id,
        code=exception.code,
        severity=exception.severity,
        title=exception.title,
        detail=exception.detail,
        blocking=bool(exception.blocking),
        status=ExceptionStatus(exception.status),
        context=exception.context or {},
        resolution_note=exception.resolution_note,
        resolved_by=exception.resolved_by,
        resolved_at=exception.resolved_at,
        created_at=exception.created_at,
    )


def to_note(note: Any) -> NoteOut:
    return NoteOut(id=note.id, author=note.author, body=note.body, created_at=note.created_at)


def to_analysis(analysis: Any) -> AnalysisOut:
    return AnalysisOut(
        kind=analysis.kind,
        provider=analysis.provider,
        model=analysis.model,
        confidence=_as_float(analysis.confidence),
        status=analysis.status,
        error=analysis.error,
        result=analysis.result or {},
        updated_at=analysis.updated_at,
    )


def to_cat_event(event: Any, *, confidence: float | None, confirmed: bool) -> CatEventOut:
    return CatEventOut(
        id=event.id,
        reference=event.reference,
        name=event.name,
        event_type=event.event_type,
        severity=event.severity,
        start_date=event.start_date.isoformat(),
        end_date=event.end_date.isoformat(),
        country=event.country,
        region=event.region,
        confidence=confidence,
        confirmed=confirmed,
    )


def to_triage(row: Any) -> TriageOut:
    return TriageOut(
        categories=list(row.categories or []),
        route=row.recommended_route,
        route_key=row.recommended_route_key,
        priority=row.recommended_priority,
        required_skill=row.required_skill,
        required_team=row.required_team,
        confidence=_as_float(row.confidence),
        reasoning=row.reasoning,
        factors=list(row.factors or []),
        overridden=bool(row.overridden),
        overridden_by=row.overridden_by,
        override_reason=row.override_reason,
        original_route=row.original_route,
    )


def to_assignment(row: Any) -> AssignmentOut:
    return AssignmentOut(
        handler_id=row.handler_id,
        handler_name=row.handler_name,
        team=row.team,
        queue=row.queue,
        status=row.status,
        strategy=row.strategy,
        confidence=_as_float(row.confidence),
        reasoning=row.reasoning,
        alternatives=list(row.alternatives or []),
        assigned_by=row.assigned_by,
        assigned_at=row.assigned_at,
    )


def to_audit_event(event: Any) -> AuditEventOut:
    return AuditEventOut(
        id=event.id,
        entity_type=event.entity_type,
        entity_reference=event.entity_reference,
        event_type=event.event_type,
        summary=event.summary,
        actor=event.actor,
        actor_type=event.actor_type,
        before=event.before,
        after=event.after,
        context=event.context or {},
        occurred_at=event.occurred_at,
    )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
