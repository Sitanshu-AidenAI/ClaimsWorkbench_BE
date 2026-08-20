"""Request and response shapes for the policy library.

Mapped explicitly, like every other schema module here, and for the reason
`mail_intake` gives: `policy_documents.extracted_text` holds the full text of a
carrier's wording, and none of that should reach a client because a column was added.
What is published is the metadata, the ingestion state, and — on a match — the
handful of passages that explain it.

**The excerpts are the exception, and they are deliberate.** A match response carries
policy text. That is the whole point of the feature: a card claiming 87% and quoting
nothing is an assertion. The bound is per-passage and per-policy, set by
`excerpts_per_policy` and `EXCERPT_CHARACTERS`, so a match returns a few paragraphs
rather than a book.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import Field

from app.domain import policy_matching as engine
from app.models.policy_document import PolicyDocument
from app.schemas.common import SchemaBase


class PolicyDocumentSummary(SchemaBase):
    """One entry in the library, as the Policies board draws it."""

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    page_count: int | None
    uploaded_by: str | None
    created_at: datetime

    #: What the document says about itself. Every one of these is `None` on a row
    #: whose ingestion has not run or has failed, which is what the screen shows
    #: instead of pretending the metadata is simply absent.
    policy_number: str | None
    insured_name: str | None
    insurer_name: str | None
    broker_name: str | None
    policy_type: str | None
    line_of_business: str | None
    effective_date: date | None
    expiry_date: date | None

    #: The book row this wording was linked to, when its number resolved to one.
    #: `None` is a first-class state: the library holds wordings for policies this
    #: deployment's book may not carry.
    policy_id: uuid.UUID | None

    # --- Ingestion ----------------------------------------------------------
    ingest_status: str
    ingest_error: str | None
    ingest_attempts: int
    ingested_at: datetime | None
    #: Both counts, always. Their difference is the whole story when the vector
    #: store is behind: 412 passages and 0 embedded is a searchable-by-keyword
    #: library, and one number could not say that.
    chunk_count: int
    embedded_chunk_count: int
    extraction_status: str
    extraction_error: str | None
    text_characters: int

    #: Additional named insureds, premises and limits read from the declarations.
    #: Shown on the row's detail; never matched on beyond what is promoted above.
    extracted_metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyDocumentListResult(SchemaBase):
    """A page of the library, with the counts the status strip needs."""

    items: list[PolicyDocumentSummary]
    total: int
    page: int
    page_size: int
    #: Documents per ingestion state, over the whole library rather than the page.
    #: The screen polls while any of `pending`, `queued` or `extracting` is non-zero,
    #: so this is what tells it when to stop.
    status_counts: dict[str, int]
    #: Passages across the library. The one number that says whether matching can
    #: work at all.
    chunk_total: int
    #: Whether an embedding provider and vector store are configured. A library that
    #: is keyword-only is working as configured, and an administrator has to be able
    #: to tell that from a library that is broken.
    semantic_search_enabled: bool


class PolicyUploadResult(SchemaBase):
    """What one upload produced."""

    document: PolicyDocumentSummary
    #: The library already held these exact bytes and the existing entry is returned.
    #: A success, so the client shows it as one — a retried request must not look
    #: like a rejection.
    duplicate: bool
    #: A worker has been asked for it. `False` means it is waiting for the sweep,
    #: which is what happens when the broker is unreachable — the upload is still
    #: safe and the policy still enters the library, a minute later.
    queued: bool


class PolicyIngestResult(SchemaBase):
    """What one re-ingestion did."""

    document: PolicyDocumentSummary
    reused: bool
    chunks: int
    embedded: int
    linked_policy_id: uuid.UUID | None
    error: str | None


# --- Matching -----------------------------------------------------------------


class PolicyMatchExcerpt(SchemaBase):
    """One passage of a wording, and why it is being shown."""

    chunk_ref: str
    content: str
    score: float
    page_number: int | None
    section_label: str | None
    #: Which of the notice's questions this passage answered.
    facet: str | None


class PolicyMatchSignal(SchemaBase):
    """One corroborating comparison, with its working shown.

    Every signal is published, not only the ones that agreed. A candidate list that
    prints only agreements is a sales pitch rather than an audit trail — the same rule
    the policy identification screen holds to, and the reason `outcome` distinguishes
    `missing` from `not_compared`: one is a gap an officer can fill and the other is a
    signal that does not apply to this risk.
    """

    signal: str
    label: str
    axis: str
    outcome: str
    weight: float
    score: float | None
    binary: bool
    explanation: str
    notice_value: str | None
    policy_value: str | None


class PolicyMatchWarningOut(SchemaBase):
    """Something true about this wording that changes no rank."""

    code: str
    detail: str


class PolicyDocumentMatch(SchemaBase):
    """One ranked wording, and everything the card draws."""

    document_id: uuid.UUID
    policy_id: uuid.UUID | None
    filename: str
    policy_number: str | None
    insured_name: str | None
    insurer_name: str | None
    broker_name: str | None
    policy_type: str | None
    line_of_business: str | None
    effective_date: date | None
    expiry_date: date | None

    rank: int
    #: The engine put this one forward. Never the same thing as a decision: nothing
    #: in this module binds a policy.
    recommended: bool
    confidence: str
    score: float
    #: Published separately from `score`, and that is deliberate. The two mean
    #: different things — one is "this wording reads like this loss" and the other is
    #: "this wording is this loss's contract" — and an officer who can see both reads
    #: a 0.7 built from prose alone very differently from a 0.7 built from a policy
    #: number.
    retrieval_score: float
    corroboration_score: float | None
    period_outcome: str

    reasons: list[str]
    signals: list[PolicyMatchSignal]
    warnings: list[PolicyMatchWarningOut]
    excerpts: list[PolicyMatchExcerpt]
    facets_matched: list[str]
    chunks_matched: int


class PolicyMatchResponse(SchemaBase):
    """The whole answer for one notice."""

    fnol_reference: str | None
    matches: list[PolicyDocumentMatch]
    #: Compared and rejected. An explanation rather than an option, which is why it
    #: is a separate list: "why is my policy not listed" needs an answer, and a
    #: rejected candidate at the bottom of `matches` would read as a weak one.
    rejected: list[PolicyDocumentMatch]
    documents_compared: int
    chunks_considered: int
    #: How many identifying values the notice actually gave us. A candidate list is
    #: only as good as what was fed to it, and this is the figure that says so.
    signals_available: int
    facets_queried: list[str]
    #: `hybrid-rrf` | `semantic` | `keyword` | `none` | `disabled`.
    strategy: str
    #: A backend that should have answered did not. Not the same as one that is
    #: simply not configured.
    degraded: bool
    #: Two candidates too close to choose between. Surfaced rather than tie-broken.
    ambiguous: bool
    recommended_document_id: uuid.UUID | None
    engine_version: str


class PolicyMatchRequest(SchemaBase):
    """Match against a notice, or against values supplied directly.

    Both forms on one endpoint because they are one operation with two sources for the
    same query. `fnol_reference` is what the review screen sends; the explicit fields
    are what an administrator uses to check a freshly loaded library without having to
    create a notice first — which is the difference between a testable ingestion and a
    hopeful one.
    """

    fnol_reference: str | None = None

    policy_number: str | None = None
    insured_name: str | None = None
    broker_name: str | None = None
    project_name: str | None = None
    contract_number: str | None = None
    loss_location: str | None = None
    loss_postcode: str | None = None
    cause_of_loss: str | None = None
    loss_description: str | None = None
    line_of_business: str | None = None
    date_of_loss: date | None = None

    #: Free text, used when nothing structured is supplied. Kept last on purpose: a
    #: structured query retrieves better because it can be split into facets, and this
    #: is the fallback rather than the interface.
    query: Annotated[str | None, Field(max_length=2_000)] = None

    def as_notice(self) -> engine.NoticeQuery:
        return engine.NoticeQuery(
            policy_number=self.policy_number,
            insured_name=self.insured_name,
            broker_name=self.broker_name,
            project_name=self.project_name,
            contract_number=self.contract_number,
            loss_location=self.loss_location,
            loss_postcode=self.loss_postcode,
            cause_of_loss=self.cause_of_loss,
            loss_description=self.loss_description or self.query,
            line_of_business=self.line_of_business,
            date_of_loss=self.date_of_loss,
            reference=self.fnol_reference,
        )

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.fnol_reference,
                self.policy_number,
                self.insured_name,
                self.broker_name,
                self.project_name,
                self.contract_number,
                self.loss_location,
                self.loss_postcode,
                self.cause_of_loss,
                self.loss_description,
                self.query,
            )
        )


# --- mappers ------------------------------------------------------------------


def to_summary(document: PolicyDocument) -> PolicyDocumentSummary:
    """One library row on the wire. Explicit, so `extracted_text` cannot leak."""
    return PolicyDocumentSummary(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        page_count=document.page_count,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
        policy_number=document.policy_number,
        insured_name=document.insured_name,
        insurer_name=document.insurer_name,
        broker_name=document.broker_name,
        policy_type=document.policy_type,
        line_of_business=document.line_of_business,
        effective_date=document.effective_date,
        expiry_date=document.expiry_date,
        policy_id=document.policy_id,
        ingest_status=str(document.ingest_status),
        ingest_error=document.ingest_error,
        ingest_attempts=document.ingest_attempts,
        ingested_at=document.ingested_at,
        chunk_count=document.chunk_count,
        embedded_chunk_count=document.embedded_chunk_count,
        extraction_status=str(document.extraction_status),
        extraction_error=document.extraction_error,
        text_characters=document.text_characters,
        extracted_metadata=document.extracted_metadata or {},
    )


def to_match(match: engine.PolicyMatch) -> PolicyDocumentMatch:
    facts = match.facts
    return PolicyDocumentMatch(
        document_id=facts.document_id,
        policy_id=facts.policy_id,
        filename=facts.filename,
        policy_number=facts.policy_number,
        insured_name=facts.insured_name,
        insurer_name=facts.insurer_name,
        broker_name=facts.broker_name,
        policy_type=facts.policy_type,
        line_of_business=facts.line_of_business,
        effective_date=facts.effective_date,
        expiry_date=facts.expiry_date,
        rank=match.rank,
        recommended=match.recommended,
        confidence=match.confidence.value,
        score=round(match.score, 4),
        retrieval_score=round(match.retrieval_score, 4),
        corroboration_score=(
            None if match.corroboration_score is None else round(match.corroboration_score, 4)
        ),
        period_outcome=match.period_outcome.value,
        reasons=list(match.reasons),
        signals=[PolicyMatchSignal(**signal.as_dict()) for signal in match.signals],
        warnings=[PolicyMatchWarningOut(**warning.as_dict()) for warning in match.warnings],
        excerpts=[PolicyMatchExcerpt(**excerpt.as_dict()) for excerpt in match.excerpts],
        facets_matched=sorted(facet.value for facet in match.facets_hit),
        chunks_matched=match.chunks_matched,
    )


def to_match_response(
    result: engine.PolicyMatchResult, *, reference: str | None = None
) -> PolicyMatchResponse:
    recommended = result.recommended
    return PolicyMatchResponse(
        fnol_reference=reference,
        matches=[to_match(item) for item in result.matches],
        rejected=[to_match(item) for item in result.rejected],
        documents_compared=result.documents_compared,
        chunks_considered=result.chunks_considered,
        signals_available=result.signals_available,
        facets_queried=[facet.value for facet in result.facets_queried],
        strategy=result.strategy,
        degraded=result.degraded,
        ambiguous=result.ambiguous,
        recommended_document_id=recommended.document_id if recommended else None,
        engine_version=result.engine_version,
    )


__all__ = [
    "PolicyDocumentListResult",
    "PolicyDocumentMatch",
    "PolicyDocumentSummary",
    "PolicyIngestResult",
    "PolicyMatchExcerpt",
    "PolicyMatchRequest",
    "PolicyMatchResponse",
    "PolicyMatchSignal",
    "PolicyMatchWarningOut",
    "PolicyUploadResult",
    "to_match",
    "to_match_response",
    "to_summary",
]
