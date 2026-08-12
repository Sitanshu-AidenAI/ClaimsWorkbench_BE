"""Request and response shapes for the claims API.

These mirror the shapes the claims queue and the claim workbench already read, so
wiring the screens to the API is a change of source rather than a redesign. Where
a field on those screens has no source in this module yet — recoveries, paid to
date — it is stated as such rather than invented: a zero that means "not tracked"
is worse on a financial screen than an absent figure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import SchemaBase
from app.schemas.fnol import (
    AssignmentOut,
    FNOLDocumentOut,
    Money,
    NoteOut,
    TriageOut,
)


class ClaimSummary(SchemaBase):
    """A row in the claims queue."""

    id: uuid.UUID
    reference: str
    claimant: str
    location: str
    loss_type: str
    status: str
    priority: str
    reported_at: datetime
    age_days: int | None
    reserve: Money
    over_authority: bool
    fraud_flag: bool
    cat_linked: bool
    handler_name: str | None
    severity: str | None
    fnol_reference: str | None


class QueueMetric(SchemaBase):
    id: str
    label: str
    #: Pre-formatted where the tile abbreviates — the server owns the rounding.
    display: str
    count: int | None


class StatusFacet(SchemaBase):
    status: str
    label: str
    count: int


class ClaimsQueueResult(SchemaBase):
    items: list[ClaimSummary]
    total: int
    page: int
    page_size: int
    metrics: list[QueueMetric]
    facets: list[StatusFacet]
    scope_summary: str


class CoverageCheckOut(SchemaBase):
    key: str
    label: str
    state: str
    detail: str


class CoverageReviewOut(SchemaBase):
    indicator: str
    confidence: float | None
    reasoning: str
    checks: list[CoverageCheckOut]
    completed_at: datetime | None
    produced_by: str


class FraudIndicatorOut(SchemaBase):
    code: str
    title: str
    detail: str
    weight: float


class FraudAssessmentOut(SchemaBase):
    level: str
    score: float
    indicators: list[FraudIndicatorOut]


class ActivityEntryOut(SchemaBase):
    id: uuid.UUID
    description: str
    actor: str
    actor_type: str
    occurred_at: datetime
    kind: Literal["decision", "assistant", "evidence"]


class ClaimDetail(SchemaBase):
    """Everything the preview pane and the workbench header read."""

    id: uuid.UUID
    reference: str
    status: str
    priority: str
    severity: str | None

    claimant_name: str | None
    insured_name: str | None
    policy_number: str | None
    policy_period: str | None

    line_of_business: str | None
    loss_type: str | None
    loss_description: str | None
    loss_location: str | None
    loss_country: str | None
    date_of_loss: datetime | None
    reported_at: datetime
    age_days: int | None

    reserve: Money
    paid: Money
    #: The assigned handler's settlement limit, and how far the reserve exceeds
    #: it. Both null while the claim is unassigned — an authority figure with
    #: nobody behind it is not a fact about the claim.
    authority_limit: Money | None
    over_authority_by: Money | None
    over_authority: bool
    fraud_flag: bool
    cat_reference: str | None

    handler_name: str | None
    created_by: str | None

    fnol_reference: str | None
    fnol_summary: str | None
    completeness_score: float | None

    triage: TriageOut | None
    assignment: AssignmentOut | None
    coverage: CoverageReviewOut | None
    fraud: FraudAssessmentOut | None

    documents: list[FNOLDocumentOut]
    notes: list[NoteOut]
    activity: list[ActivityEntryOut]


class ClaimExtractedFieldOut(SchemaBase):
    id: str
    label: str
    value: str
    confidence: float
    numeric: bool
    corrected: bool
    source_document_id: uuid.UUID | None


class ClaimRuleOut(SchemaBase):
    code: str
    label: str
    outcome: Literal["pass", "warn", "not_run"]
    detail: str | None


class ClaimWorkbench(SchemaBase):
    """The document-review workspace for one claim."""

    reference: str
    status: str
    summary_line: str
    policyholder: str
    policy_reference: str
    reserve: Money
    estimate: Money
    severity: str | None
    stage: str
    stage_note: str
    siu_referral_open: bool
    approval_blocks: list[str]

    documents: list[FNOLDocumentOut]
    extracted_fields: dict[str, list[ClaimExtractedFieldOut]]
    coverage: CoverageReviewOut | None
    rules: list[ClaimRuleOut]
    fraud: FraudAssessmentOut | None
    triage: TriageOut | None
    assignment: AssignmentOut | None
    fnol_reference: str | None


class HandlerOut(SchemaBase):
    id: uuid.UUID
    full_name: str
    email: str
    team: str
    job_title: str | None
    skills: list[str]
    lines_of_business: list[str]
    countries: list[str]
    max_severity: str
    authority_limit: Money | None
    open_claims: int
    capacity: int


class AssignmentRequest(SchemaBase):
    #: Absent means "accept the recommendation".
    handler_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=1000)


class TriageOverrideRequest(SchemaBase):
    route_key: str = Field(max_length=48)
    route_label: str = Field(max_length=96)
    priority: Literal["routine", "standard", "high", "urgent"]
    reason: str = Field(min_length=3, max_length=1000)


def to_handler(handler: Any) -> HandlerOut:
    return HandlerOut(
        id=handler.id,
        full_name=handler.full_name,
        email=handler.email,
        team=handler.team,
        job_title=handler.job_title,
        skills=list(handler.skills or []),
        lines_of_business=list(handler.lines_of_business or []),
        countries=list(handler.countries or []),
        max_severity=handler.max_severity,
        authority_limit=Money(amount_minor=handler.authority_limit_minor, currency=handler.currency)
        if handler.authority_limit_minor
        else None,
        open_claims=handler.open_claims or 0,
        capacity=handler.capacity or 0,
    )
