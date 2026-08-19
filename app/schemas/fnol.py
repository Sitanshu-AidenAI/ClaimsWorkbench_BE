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
from app.domain.policy_identification import STRENGTH_FOR_CONFIDENCE
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
    """A file on the notice — one row of the case file.

    Carries how the text was read and how far indexing got, because both are things a
    claims officer legitimately asks about a document that has produced no fields:
    "was this a scan", "was it OCR'd", "has it been read yet".
    """

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

    # --- How it was read -----------------------------------------------------
    text_extractor: str | None = None
    ocr_applied: bool = False
    ocr_confidence: float | None = None
    #: The detector's sentence, so the screen can say *why* rather than just "OCR: yes".
    ocr_reason: str | None = None

    # --- How far indexing got ------------------------------------------------
    index_status: str = "pending"
    #: Below `chunk_count` means the vector index is behind the passages. Surfaced
    #: rather than hidden: the drift is repairable and invisible drift is not.
    chunk_count: int = 0
    embedded_chunk_count: int = 0
    index_error: str | None = None
    indexed_at: datetime | None = None


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
    #: The passage the value was read from. Present means the review screen can offer
    #: "show me where this came from" for this field, and
    #: `GET /fnol/{reference}/fields/{path}/evidence` is what that button calls.
    source_chunk_id: uuid.UUID | None = None
    source_document_filename: str | None = None
    source_page_number: int | None = None


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


class SignalResultOut(SchemaBase):
    """One signal compared against one policy, with its working shown.

    `evidence_field_key` is what makes a reason clickable: it names the extraction
    dataset field the notice's side of this signal was read from, which the review
    screen hands to the evidence endpoint to open the page and mark the passage. A
    signal with none — the envelope's own sender domain — says so by holding null,
    and the panel simply does not offer a link rather than offering a dead one.
    """

    signal: str
    label: str
    #: `policy` | `insured` | `broker` | `project` | `risk` | `cover`. The axis of
    #: identity this signal speaks to, which is how the panel groups them.
    axis: str
    #: `match` | `partial` | `mismatch` | `missing` | `not_compared`. `missing` and
    #: `not_compared` are separate: the first is a gap in the notice an officer can
    #: fill, the second is a signal that does not apply to this risk.
    outcome: str
    weight: float
    #: The number the weighted mean used. Null only when nothing was compared.
    score: float | None
    #: This signal's answer is a yes or a no, so the panel prints no percentage
    #: beside it — "100%" against "the loss date falls inside the policy period" is a
    #: number pretending to be a measurement.
    binary: bool = False
    explanation: str
    notice_value: str | None
    policy_value: str | None
    evidence_field_key: str | None
    document_id: uuid.UUID | None
    page_number: int | None
    quote: str | None


class CandidateWarningOut(SchemaBase):
    """Something about the cover rather than about the identification.

    Kept apart from the signals because a candidate can be certainly the right
    policy and a poor answer to this loss at the same time, and collapsing the two
    into one percentage hides the thing the officer most needs to see.
    """

    code: str
    detail: str
    severity: str


class CandidateDisplayOut(SchemaBase):
    """The candidate card's face, formatted once on the server.

    `limit` and `excess` are the *matched location's* where the schedule gives it
    one, because the sum insured and the deductible attach to the location rather
    than to the policy — a card printing the policy's headline excess beside a loss
    at a location with its own is printing the wrong number.
    """

    insured_name: str
    line_of_business: str
    policy_type: str | None
    policy_period: str
    status: str
    limit: Money | None
    excess: Money | None
    location: str | None
    #: "Location 007", as printed on the schedule, when a scheduled entry matched.
    location_label: str | None
    broker_name: str | None
    project_name: str | None
    contract_number: str | None


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

    # -- Identification --------------------------------------------------------
    rank: int = 0
    #: `exact` | `strong` | `possible` | `weak` | `rejected`. Finer than
    #: `match_strength`, which stays as the coarse band the queue filters on.
    confidence: str = "possible"
    #: `in_force` | `in_maintenance_period` | `prior_term` | `outside_period` |
    #: `unknown`.
    period_outcome: str = "unknown"
    signals: list[SignalResultOut] = Field(default_factory=list)
    warnings: list[CandidateWarningOut] = Field(default_factory=list)
    display: CandidateDisplayOut | None = None
    #: `engine` | `officer_search`. A candidate a person found rather than one the
    #: engine ranked, which the card says out loud.
    origin: str = "engine"
    recommended: bool = False
    recommendation_reason: str | None = None
    compared_signal_count: int = 0
    signal_count: int = 0
    selected_at: datetime | None = None


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
    #: Where identification has got to, as its own axis. `policy_confirmed` says
    #: whether a policy is bound; this says whether the *question* is settled, and a
    #: notice referred with no policy is settled without one.
    policy_identification_status: str
    policy_referral_reason: str | None
    #: The notice's side of the identification signals, so the review screen can show
    #: them without a second call.
    broker_name: str | None
    broker_reference: str | None
    risk_location: str | None
    loss_postcode: str | None
    project_name: str | None
    contract_number: str | None
    policy_period_stated: str | None

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
    #: The whole queue behind the page in `items`, after filtering.
    total: int
    #: Which page of that queue `items` is, and how big a page is. The figures and
    #: insights alongside describe the whole book regardless of either.
    page: int = 1
    page_size: int = 8
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
    #: What the indexing stage did, and whether the extraction read retrieved passages
    #: or the whole corpus — so "is retrieval actually doing anything here" is
    #: answerable from the response rather than only from the logs.
    documents_indexed: int = 0
    chunks_indexed: int = 0
    retrieval_used: bool = False


# --- Document passages and evidence -------------------------------------------


class HighlightRectOut(SchemaBase):
    """One rectangle to draw over a page, in PDF points from the top-left.

    `page_width` and `page_height` travel with every rectangle so a viewer can scale to
    whatever size it renders at without a second request asking how big the page is.
    """

    page_number: int
    x0: float
    top: float
    x1: float
    bottom: float
    page_width: float
    page_height: float


class DocumentChunkOut(SchemaBase):
    """One passage of a document."""

    id: uuid.UUID
    chunk_ref: str
    chunk_index: int
    content: str
    token_count: int
    char_start: int
    char_end: int
    page_number: int | None
    page_from: int | None
    page_to: int | None
    section_label: str | None
    embedded: bool


class DocumentChunkListResult(SchemaBase):
    items: list[DocumentChunkOut]
    total: int
    page: int
    page_size: int


class DocumentSearchHitOut(SchemaBase):
    """One passage that matched a search, and how it was found."""

    chunk_ref: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page_number: int | None
    section_label: str | None
    snippet: str
    score: float
    semantic_score: float | None
    keyword_score: float | None


class DocumentSearchResult(SchemaBase):
    items: list[DocumentSearchHitOut]
    #: `hybrid-rrf`, `semantic`, `keyword`, or `none`. Reported so a thin result set is
    #: interpretable — "keyword" means the vector index was unavailable or unconfigured.
    strategy: str
    degraded: bool
    chunks_searched: int


class FieldEvidenceOut(SchemaBase):
    """Where one extracted value came from.

    This is the payload behind "show me where this came from". It answers at three
    levels of precision and says which one it reached: the document and page always,
    the exact text always, and rectangles when the document is a PDF whose words could
    be located. `note` explains the absence of rectangles rather than leaving a viewer
    to render a page with nothing marked on it.
    """

    path: str
    label: str
    value: str | None
    confidence: float | None
    source: str
    human_modified: bool

    document_id: uuid.UUID | None
    filename: str | None
    content_type: str | None

    chunk_id: uuid.UUID | None
    chunk_ref: str | None
    page_number: int | None
    section_label: str | None

    #: The text to highlight — the model's quoted evidence when it could be located
    #: inside the passage, otherwise the passage itself, capped.
    text: str | None
    #: Offsets into the document's stored text, for a plain-text viewer.
    char_start: int | None
    char_end: int | None
    rects: list[HighlightRectOut]
    note: str | None


class DocumentIndexRequest(SchemaBase):
    """Ask for a case's documents to be read into passages."""

    #: Re-read and re-index even when nothing has changed. The normal path is
    #: fingerprint-guarded and does nothing when the inputs hold still.
    force: bool = False
    #: One document rather than all of them.
    document_id: uuid.UUID | None = None
    #: Run the FNOL pipeline afterwards, which is what turns new passages into fields.
    run_pipeline: bool = True


class DocumentIndexResult(SchemaBase):
    reference: str
    processing_state: str
    documents: int
    indexed: int
    skipped: int
    failed: int
    reused: int
    chunks: int
    embedded: int
    errors: list[str]


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
    term: str
    #: True when nothing in the book matched the term at all, which is a different
    #: answer from "the engine found nothing" and is worth saying differently.
    exhausted: bool = False


class ExtractedSignalOut(SchemaBase):
    """One thing the notice gave the engine to search on, or did not.

    Rendered as the "what we matched on" panel, and rendered even when nothing
    matched: an officer who can see the search ran on a policy number and an insured
    name knows the book is the problem rather than the reading.
    """

    signal: str
    label: str
    axis: str
    #: `match` when the notice stated it, `missing` when it did not. Never a
    #: judgement about a policy — this panel is about the notice alone.
    outcome: str
    value: str | None
    explanation: str
    confidence: float | None
    weight: float
    evidence_field_key: str | None
    document_id: uuid.UUID | None
    page_number: int | None
    quote: str | None


class PolicyIdentificationOut(SchemaBase):
    """The whole first-stage answer for one notice, in one call.

    One request rather than four, for the same reason the review workspace reads a
    whole case in one: a screen assembled from four calls shows four different
    moments of the same decision.
    """

    reference: str
    #: `not_run` | `no_match` | `needs_review` | `confident_match` | `confirmed` |
    #: `referred`.
    status: str
    #: The coarse band the queue and the exception engine work from.
    strength: str
    ran_at: datetime | None
    engine_version: str | None
    policies_compared: int

    #: What the notice said, signal by signal, including what it did not say.
    extracted_signals: list[ExtractedSignalOut]
    signals_present: int
    signals_missing: int

    candidates: list[PolicyCandidateOut]
    #: Compared and rejected, kept because "why is my policy not in the list" needs
    #: an answer and an officer recognises the right policy at 0.3 more often than
    #: the arithmetic does. Never ranked with the others; confirming one goes through
    #: the same path as confirming a searched policy.
    near_misses: list[PolicyCandidateOut]
    recommended_policy_id: uuid.UUID | None
    selected_policy_id: uuid.UUID | None
    policy_confirmed: bool
    confirmed_by: str | None
    confirmed_at: datetime | None
    referral_reason: str | None
    referred_by: str | None
    #: True once the decision is settled either way — a policy bound, or the notice
    #: referred with no policy identified. What the decision bar gates on.
    decided: bool


class PolicyReferralRequest(SchemaBase):
    """No policy could be identified. A decision, so it carries a reason."""

    reason: str = Field(min_length=3, max_length=2000)


class FNOLDeletionResult(SchemaBase):
    """The receipt for a deleted notification.

    A body rather than `204 No Content`, because the officer who pressed the
    button is entitled to know what actually went — and because the two things
    that can partly fail, object storage and the search index, have no other way
    to report themselves once the rows are committed.
    """

    reference: str
    deleted: bool
    #: One entry per table: `documents`, `chunks`, `fields`, `extracted_values`,
    #: `mail_messages` and so on.
    records: dict[str, int]
    #: Every row across every table the notice owned.
    total_records: int
    #: Files removed from object storage, and how many were expected.
    documents_stored: int
    blobs_removed: int
    #: Whether the vector index was swept. `false` when none is configured.
    vectors_cleared: bool
    #: The audit trail is kept. Named in the response so nobody has to infer it.
    audit_retained: bool = True
    #: Non-fatal problems, fit to show the officer.
    warnings: list[str] = Field(default_factory=list)


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
        text_extractor=document.text_extractor,
        ocr_applied=bool(document.ocr_applied),
        ocr_confidence=_as_float(document.ocr_confidence),
        ocr_reason=document.ocr_reason,
        index_status=document.index_status,
        chunk_count=document.chunk_count or 0,
        embedded_chunk_count=document.embedded_chunk_count or 0,
        index_error=document.index_error,
        indexed_at=document.indexed_at,
    )


def to_field(
    field: Any,
    citations: dict[uuid.UUID, tuple[str, int | None]] | None = None,
) -> FNOLFieldOut:
    """One field row.

    `citations` maps a chunk id to `(filename, page_number)`. Passed in rather than
    read through the relationship because rendering the review screen must not fire one
    query per field — and because `FNOLDocumentChunk` is deliberately `lazy="raise"`,
    so a lazy read here would be an error rather than a slow success.
    """
    filename: str | None = None
    page_number: int | None = None
    if citations and field.source_chunk_id is not None:
        found = citations.get(field.source_chunk_id)
        if found is not None:
            filename, page_number = found

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
        source_chunk_id=field.source_chunk_id,
        source_document_filename=filename,
        source_page_number=page_number,
    )


def to_chunk(chunk: Any) -> DocumentChunkOut:
    return DocumentChunkOut(
        id=chunk.id,
        chunk_ref=chunk.chunk_ref,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        token_count=chunk.token_count,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        page_number=chunk.page_number,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        section_label=chunk.section_label,
        embedded=chunk.vector_point_id is not None,
    )


#: Characters of a passage shown in a search result. Enough to judge relevance, short
#: enough that twenty hits are readable.
SEARCH_SNIPPET_CHARACTERS = 320


def to_search_hit(hit: Any, *, filename: str) -> DocumentSearchHitOut:
    chunk = hit.chunk
    return DocumentSearchHitOut(
        chunk_ref=chunk.chunk_ref,
        chunk_id=chunk.id,
        document_id=chunk.fnol_document_id,
        filename=filename,
        page_number=chunk.page_number,
        section_label=chunk.section_label,
        snippet=chunk.content[:SEARCH_SNIPPET_CHARACTERS],
        score=hit.score,
        semantic_score=hit.semantic_score,
        keyword_score=hit.keyword_score,
    )


def to_highlight_rect(rect: Any) -> HighlightRectOut:
    return HighlightRectOut(
        page_number=rect.page_number,
        x0=rect.x0,
        top=rect.top,
        x1=rect.x1,
        bottom=rect.bottom,
        page_width=rect.page_width,
        page_height=rect.page_height,
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
    """A persisted candidate, or a policy nobody has scored yet.

    Both shapes go through one mapper so the frontend draws one list. A policy with
    no candidate row — one an officer has just found in the book — reports honestly
    as having nothing compared, rather than being dressed up as a zero-scoring match.
    """
    signals = [_signal_result(entry) for entry in (match.signals or [])] if match else []
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
        rank=int(match.rank) if match else 0,
        confidence=match.confidence if match else "possible",
        period_outcome=match.period_outcome if match else "unknown",
        signals=signals,
        warnings=[
            CandidateWarningOut(
                code=str(entry.get("code", "")),
                detail=str(entry.get("detail", "")),
                severity=str(entry.get("severity", "warning")),
            )
            for entry in (match.warnings or [])
        ]
        if match
        else [],
        display=_candidate_display(match.display, policy) if match else None,
        origin=match.origin if match else "officer_search",
        recommended=bool(match.recommended) if match else False,
        recommendation_reason=None,
        compared_signal_count=sum(
            1
            for entry in signals
            if entry.outcome in ("match", "partial", "mismatch")
        ),
        signal_count=len(signals),
        selected_at=match.selected_at if match else None,
    )


def to_scored_candidate(candidate: Any, policy: Any) -> PolicyCandidateOut:
    """A candidate the engine has just scored but nothing has persisted.

    The manual search path: an officer's search is *looking*, not deciding, so
    nothing is written until they confirm — but the results still carry the full
    per-signal working, because a fallback that showed less than the thing it falls
    back from would be a worse tool.
    """
    display = candidate.display.as_dict()
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
        match_strength=STRENGTH_FOR_CONFIDENCE[candidate.confidence].value,
        score=round(candidate.score, 4),
        matched_on={
            result.signal: round(result.score, 3)
            for result in candidate.signal_results
            if result.compared and result.score is not None
        },
        reasoning=candidate.recommendation_reason or None,
        selected=False,
        selected_by=None,
        rank=candidate.rank,
        confidence=candidate.confidence.value,
        period_outcome=candidate.period_outcome.value,
        signals=[_signal_result(result.as_dict()) for result in candidate.signal_results],
        warnings=[
            CandidateWarningOut(
                code=warning.code, detail=warning.detail, severity=warning.severity
            )
            for warning in candidate.warnings
        ],
        display=_candidate_display(display, policy),
        origin="officer_search",
        recommended=False,
        recommendation_reason=candidate.recommendation_reason or None,
        compared_signal_count=len(candidate.compared_signals),
        signal_count=len(candidate.signal_results),
        selected_at=None,
    )


def _signal_result(entry: dict[str, Any]) -> SignalResultOut:
    return SignalResultOut(
        signal=str(entry.get("signal", "")),
        label=str(entry.get("label", "")),
        axis=str(entry.get("axis", "policy")),
        outcome=str(entry.get("outcome", "not_compared")),
        weight=float(entry.get("weight", 0.0) or 0.0),
        score=None if entry.get("score") is None else float(entry["score"]),
        binary=bool(entry.get("binary", False)),
        explanation=str(entry.get("explanation", "")),
        notice_value=_text_or_none(entry.get("notice_value")),
        policy_value=_text_or_none(entry.get("policy_value")),
        evidence_field_key=_text_or_none(entry.get("evidence_field_key")),
        document_id=_uuid_or_none(entry.get("document_id")),
        page_number=entry.get("page_number"),
        quote=_text_or_none(entry.get("quote")),
    )


def _candidate_display(stored: dict[str, Any] | None, policy: Any) -> CandidateDisplayOut:
    """The card's face, falling back to the policy row when nothing was stored.

    The fallback matters for a candidate written before the engine existed, and for
    one an officer found by hand: an empty card would be a worse answer than the
    policy's own headline figures.
    """
    stored = stored or {}
    currency = str(stored.get("currency") or policy.currency or "GBP")
    return CandidateDisplayOut(
        insured_name=str(stored.get("insured_name") or policy.insured_name),
        line_of_business=str(stored.get("line_of_business") or policy.line_of_business),
        policy_type=_text_or_none(stored.get("policy_type")) or policy.policy_type,
        policy_period=str(
            stored.get("policy_period")
            or f"{policy.effective_date:%d %b %Y} – {policy.expiry_date:%d %b %Y}"
        ),
        status=str(stored.get("status") or policy.status),
        limit=money(
            stored.get("limit_minor")
            if stored.get("limit_minor") is not None
            else policy.limit_amount_minor,
            currency,
        ),
        excess=money(
            stored.get("excess_minor")
            if stored.get("excess_minor") is not None
            else policy.deductible_amount_minor,
            currency,
        ),
        location=_text_or_none(stored.get("location")) or policy.primary_location,
        location_label=_text_or_none(stored.get("location_label")),
        broker_name=_text_or_none(stored.get("broker_name")) or policy.broker_name,
        project_name=_text_or_none(stored.get("project_name")) or policy.project_name,
        contract_number=_text_or_none(stored.get("contract_number")) or policy.contract_number,
    )


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


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
