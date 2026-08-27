"""Request and response shapes for the claims API.

These mirror the shapes the claims queue and the claim workbench already read, so
wiring the screens to the API is a change of source rather than a redesign. Where
a field on those screens has no source in this module yet — recoveries, paid to
date — it is stated as such rather than invented: a zero that means "not tracked"
is worse on a financial screen than an absent figure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.domain.claim_lifecycle import COMMITTING_DECISIONS
from app.domain.enums import (
    ClaimDecisionAction,
    ClaimPartyRole,
    CoverageStandpoint,
    DamageSeverity,
    DeductibleType,
    FraudDisposition,
    InspectionStatus,
    MovementType,
    NoteSection,
    RecoveryKind,
    RecoveryStatus,
    SiuStatus,
)
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


class ApprovalBlockOut(SchemaBase):
    """One thing standing between this claim and a settlement.

    The `reason` is the sentence a handler reads; the `code` is what an interface
    acts on. A screen offering "assign a handler" beside the block that says so
    needs the second — matching on the first breaks the moment the wording
    improves.
    """

    code: str
    reason: str


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
    approval_blocks: list[ApprovalBlockOut]

    documents: list[FNOLDocumentOut]
    extracted_fields: dict[str, list[ClaimExtractedFieldOut]]
    coverage: CoverageReviewOut | None
    rules: list[ClaimRuleOut]
    fraud: FraudAssessmentOut | None
    triage: TriageOut | None
    assignment: AssignmentOut | None
    fnol_reference: str | None


class HandlerOut(SchemaBase):
    """Somebody a claim can be put on the desk of.

    `has_account` is the field worth reading twice. A directory row without a
    `subject` is a person with no way to sign in — seed data, or a colleague
    recorded before they were onboarded — and a claim assigned to them leaves the
    manager's queue and arrives on nobody's. Assigning to one is still allowed,
    because a desk does record work against somebody joining on Monday; what is not
    allowed is doing it without being told, so the dialog says which is which.
    """

    id: uuid.UUID
    full_name: str
    email: str
    #: Whether this person can sign in and therefore be *shown* the claim.
    has_account: bool
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


class ClaimNoteRequest(SchemaBase):
    """A note on the claim.

    `section` says which tab it was written on and defaults to the claim itself, so
    the general case is a one-field body. Typed against `NoteSection` rather than
    left a free string: a note filed under a section the screen does not render is
    a note nobody reads again.
    """

    body: str = Field(min_length=1, max_length=4000)
    section: NoteSection = NoteSection.GENERAL


class ReserveMovementRequest(SchemaBase):
    """A movement on the reserve.

    **`amount_minor` is a delta, not a new total, and it is signed.** Raising a
    reserve from £40,000 to £60,000 is `+2000000`; releasing £5,000 of it is
    `-500000`. This is the single most misreadable field in the claims API, which is
    why the model refuses zero outright rather than accepting a no-op movement that
    would sit in the ledger explaining nothing.

    `rationale` is required. An unexplained reserve movement is the thing an auditor
    asks about most reliably, and the only moment the answer is cheap to capture is
    the moment the movement is made.

    The accounting pair is optional and must be given whole. A converted figure
    without its currency is not a figure, and a currency without an amount is not a
    conversion — so the two are validated together rather than independently.
    """

    movement_type: MovementType
    #: CLAWS's second level. Free text until Attachment 1 supplies the code list.
    sub_movement_type: str | None = Field(default=None, max_length=64)
    amount_minor: int
    #: Absent means the claim's own currency, which is the overwhelmingly common
    #: case. Stated explicitly when a movement is incurred in another one.
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    accounting_amount_minor: int | None = None
    accounting_currency: str | None = Field(default=None, min_length=3, max_length=3)
    rationale: str = Field(min_length=3, max_length=2000)
    basis: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _check(self) -> ReserveMovementRequest:
        if self.amount_minor == 0:
            raise ValueError(
                "A reserve movement is a change to what is held, so it cannot be zero. "
                "Use a positive amount to raise the reserve and a negative one to release it."
            )
        if (self.accounting_amount_minor is None) != (self.accounting_currency is None):
            raise ValueError(
                "Give the accounting amount and the accounting currency together, or "
                "neither. A converted figure without its currency is not a figure."
            )
        return self


class CoverageStandpointRequest(SchemaBase):
    """Take, or change, a position on one section of the policy.

    `reason` is conditionally required, and the condition is enforced in the
    service rather than here: it depends on what the *rules proposed* for this
    particular section, which a request body cannot know. Confirming a proposal
    needs no justification; departing from it does, and the service answers 422
    naming both positions when it is missing.

    Every field but `standpoint` is optional and absent means "leave it". That
    makes this a patch rather than a put, which matters because the tab lets a
    handler set a claimed figure and a standpoint in separate actions, and a put
    would have the second silently clear the first.
    """

    standpoint: CoverageStandpoint
    reason: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=2000)
    #: What is being claimed under this section. Absent leaves it unchanged.
    claimed_minor: int | None = Field(default=None, ge=0)
    #: The section's own limit, where it is narrower than the policy's. Absent
    #: leaves it unchanged; the policy book cannot supply one.
    sublimit_minor: int | None = Field(default=None, ge=0)


class ClaimPartyRequest(SchemaBase):
    """Add somebody the notification never named.

    The three roles this exists for — underwriter, internal handler, loss adjuster
    — are the ones CLAWS entry category 5 screens and no broker's email mentions.
    Everything the extraction found is already on the claim, copied at creation.
    """

    role: ClaimPartyRole
    name: str = Field(min_length=1, max_length=255)
    organisation: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)


class CoveragePartyLinkRequest(SchemaBase):
    """Link one party to one coverage section. CLAWS entry category 6."""

    party_id: uuid.UUID
    #: Why they are on this section — "named insured", "injured employee".
    basis: str | None = Field(default=None, max_length=160)


class DeductibleRequest(SchemaBase):
    """Record the excess, on the claim or on one section.

    `section_key` absent means the whole claim, which is the common case: most
    policies carry a single contract-level excess. Naming a section scopes it there
    instead, and setting the same scope twice replaces rather than accumulates —
    a claim has one excess per scope, and two rows would leave the arithmetic to
    pick one.

    Note `deductible_type` is required and has no default. The policy book records
    an amount and not a type, so claim creation assumes per-claim and says so in
    the row's comment; a handler correcting it has to state what it actually is
    rather than accept a silent default twice.
    """

    deductible_type: DeductibleType
    amount_minor: int = Field(ge=0)
    section_key: str | None = Field(default=None, max_length=64)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    #: CLAWS's cap on the deduction. Not the excess — a percentage excess capped
    #: at a figure needs both numbers.
    maximum_applied_minor: int | None = Field(default=None, ge=0)
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _cap_above_zero_is_meaningful(self) -> DeductibleRequest:
        if (
            self.maximum_applied_minor is not None
            and self.maximum_applied_minor < self.amount_minor
            and self.deductible_type is not DeductibleType.AGGREGATE
        ):
            raise ValueError(
                "The cap is below the excess, which would make the excess "
                "unreachable. Either raise the cap or lower the excess — unless "
                "this is an aggregate, where a lower cap is meaningful."
            )
        return self


class ClaimDecisionRequest(SchemaBase):
    """A decision taken from the workbench's foot or the queue's preview pane.

    `reason` is required for the two decisions that end the claim and optional for
    the three that do not, which is enforced here rather than in the route so that
    the rule is visible in the API documentation. Declining a claim without a
    recorded reason is the one write on this API a complaint would be built out of.
    """

    decision: ClaimDecisionAction
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_required_to_commit(self) -> ClaimDecisionRequest:
        if self.decision in COMMITTING_DECISIONS and not (self.reason or "").strip():
            raise ValueError(
                f"Give a reason. '{self.decision.value}' closes the claim, and a "
                "closing decision with no recorded reason cannot be explained later."
            )
        return self


# ---------------------------------------------------------------------------
# The field inspection
# ---------------------------------------------------------------------------


def _as_utc(value: datetime | None) -> datetime | None:
    """A naive timestamp is read as UTC rather than refused.

    Every column this reaches is `timestamptz`, and a naive value would land at
    whatever the session's zone happens to be. Browsers send an offset; scripts and
    curl frequently do not, and refusing them would buy a correctness the caller
    cannot see and did not ask for.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class InspectionCommissionRequest(SchemaBase):
    """Instruct a loss adjuster to attend.

    Every field is optional, deliberately. A desk commissioning a visit at four in
    the afternoon frequently knows only that one is needed - the firm is chosen
    tomorrow, and the adjuster names themselves when they acknowledge. Requiring the
    adjuster here would push that reality into a free-text note, which is the
    failure this record exists to end.

    **The site defaults to the claim's loss location** and to nothing else. See
    `ClaimInspectionService.commission`.

    `scheduled_at` is accepted as a convenience and is not the same act: when it is
    given, the claim gets two audit events - commissioned, then booked - because
    that is what happened. Leave it out and the inspection sits at *to schedule*,
    which is an honest state and the common one.
    """

    adjuster_name: str | None = Field(default=None, max_length=255)
    adjuster_firm: str | None = Field(default=None, max_length=255)
    scheduled_at: datetime | None = None
    report_due_at: datetime | None = None

    site_kind: str | None = Field(default=None, max_length=64)
    site_address: str | None = Field(default=None, max_length=2000)
    site_identifier: str | None = Field(default=None, max_length=128)
    site_contact_name: str | None = Field(default=None, max_length=255)
    site_contact_phone: str | None = Field(default=None, max_length=64)
    site_access_note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _normalise(self) -> InspectionCommissionRequest:
        object.__setattr__(self, "scheduled_at", _as_utc(self.scheduled_at))
        object.__setattr__(self, "report_due_at", _as_utc(self.report_due_at))
        return self


class InspectionScheduleRequest(SchemaBase):
    """Book the visit, or move it.

    The adjuster's details are accepted again because this is usually when they
    arrive: a desk instructs a firm, and the named adjuster and their own reference
    come back with the acknowledgement. Omitted fields are left as they were rather
    than cleared - a rebooking must not wipe the adjuster.
    """

    scheduled_at: datetime
    adjuster_name: str | None = Field(default=None, max_length=255)
    adjuster_firm: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=64)
    report_due_at: datetime | None = None

    @model_validator(mode="after")
    def _normalise(self) -> InspectionScheduleRequest:
        object.__setattr__(self, "scheduled_at", _as_utc(self.scheduled_at))
        object.__setattr__(self, "report_due_at", _as_utc(self.report_due_at))
        return self


class InspectionAttendanceRequest(SchemaBase):
    """The adjuster went, and what they came back with, counted.

    A future `attended_at` is refused: it would mean the visit has happened when it
    has not, and every figure the tab derives from attendance would then be reading
    a plan as a fact. A booking that has not happened yet is a booking - move its
    date instead.

    The four counts are absent-means-unchanged rather than absent-means-zero, so a
    desk correcting the photograph count does not silently wipe the statements.
    """

    attended_at: datetime
    summary: str | None = Field(default=None, max_length=8000)
    photographs: int | None = Field(default=None, ge=0, le=10_000)
    measurements: int | None = Field(default=None, ge=0, le=10_000)
    statements: int | None = Field(default=None, ge=0, le=10_000)
    documents: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _not_in_the_future(self) -> InspectionAttendanceRequest:
        attended = _as_utc(self.attended_at)
        if attended is not None and attended > datetime.now(UTC):
            raise ValueError(
                "That attendance date is in the future. A visit that has not "
                "happened yet is a booking, not an attendance."
            )
        object.__setattr__(self, "attended_at", attended)
        return self


class InspectionStatusRequest(SchemaBase):
    """Move the inspection, for the judgements no date implies.

    Chiefly sending a report back for more (`more_needed`) and accepting it
    (`completed`). Both are a person's reading of a report, which is why neither
    falls out of recording a timestamp.
    """

    status: InspectionStatus
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_when_sending_it_back(self) -> InspectionStatusRequest:
        if self.status is InspectionStatus.MORE_NEEDED and not (self.reason or "").strip():
            raise ValueError(
                "Say what more is needed. An adjuster told only that their report "
                "was rejected cannot act on it."
            )
        return self


class InspectionObservationRequest(SchemaBase):
    """One element the adjuster looked at, and what they found.

    The amount and its currency travel together or not at all - the same rule
    `ReserveMovementRequest` follows, for the same reason: a figure with no currency
    is not an amount. Omitting both is normal and means *not costed on site*, which
    is different from zero and is counted differently by
    `app.domain.inspection.quantified_minor`.
    """

    element: str = Field(min_length=1, max_length=255)
    severity: DamageSeverity
    finding: str = Field(min_length=1, max_length=4000)
    quantified_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    photo_count: int = Field(default=0, ge=0, le=10_000)

    @field_validator("element", "finding")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("This cannot be blank.")
        return trimmed

    @model_validator(mode="after")
    def _money_travels_in_pairs(self) -> InspectionObservationRequest:
        if (self.quantified_minor is None) != (self.currency is None):
            raise ValueError(
                "Give the amount and its currency together, or neither. An "
                "observation with no figure is one the adjuster did not price, "
                "which is recorded as such rather than as zero."
            )
        return self


class InspectionActionRequest(SchemaBase):
    """Something that has to happen before the inspection is done with.

    `owner` is a name rather than a handler id, because the owner is as often the
    adjuster, the insured or a contractor as somebody on the desk - see
    `ClaimInspectionAction`.
    """

    label: str = Field(min_length=1, max_length=255)
    owner: str = Field(min_length=1, max_length=255)
    due_at: datetime | None = None

    @field_validator("label", "owner")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("This cannot be blank.")
        return trimmed

    @model_validator(mode="after")
    def _normalise(self) -> InspectionActionRequest:
        object.__setattr__(self, "due_at", _as_utc(self.due_at))
        return self


class InspectionActionUpdateRequest(SchemaBase):
    """Tick or untick one action.

    Un-ticking is allowed. A follow-up marked complete that turns out not to be is
    an ordinary correction, and a one-way tick would make the desk raise a duplicate
    action to say so.
    """

    done: bool


# ---------------------------------------------------------------------------
# Recoveries
# ---------------------------------------------------------------------------


class RecoveryOpenRequest(SchemaBase):
    """Identify something worth pursuing.

    `expected_minor` defaults to nought and that is honest: a subrogation spotted on
    the day of the loss has no figure against it yet, and the desk revises it as the
    quantum firms up.

    `prospects` is absent rather than nought when nobody has judged it — the one
    field on this form where a default would state a conclusion. The currency is the
    claim's and the server refuses any other, because a register in two currencies
    has no total anybody can state.
    """

    kind: RecoveryKind
    label: str = Field(min_length=1, max_length=255)
    expected_minor: int = Field(default=0, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    #: 0..1. Absent means nobody has assessed it, which is not the same as nought.
    prospects: float | None = Field(default=None, ge=0.0, le=1.0)
    position: str | None = Field(default=None, max_length=4000)
    #: When the recovery is barred. Missing it costs the whole recovery.
    limitation_at: datetime | None = None

    party_name: str | None = Field(default=None, max_length=255)
    party_role: str | None = Field(default=None, max_length=64)
    party_carrier: str | None = Field(default=None, max_length=255)
    party_carrier_reference: str | None = Field(default=None, max_length=128)
    party_contact: str | None = Field(default=None, max_length=255)

    @field_validator("label")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Say what is being pursued.")
        return trimmed

    @model_validator(mode="after")
    def _normalise(self) -> RecoveryOpenRequest:
        object.__setattr__(self, "limitation_at", _as_utc(self.limitation_at))
        return self


class RecoveryProgressRequest(SchemaBase):
    """Move a recovery on, bank what came back, or revise the expectation.

    Every field optional, because these arrive one at a time as news does. At least
    one of them has to say something, which the service enforces rather than this
    schema — the useful error there names what was expected, and a schema-level
    "at least one field" message cannot.

    **`recovered_minor` is a running total, not an increment.** A second payment is
    this call with the new total, and the server refuses a figure lower than what is
    already recorded: that is a correction, and a correction to a money column
    belongs in a ledger rather than an overwrite.
    """

    status: RecoveryStatus | None = None
    note: str | None = Field(default=None, max_length=4000)
    expected_minor: int | None = Field(default=None, ge=0)
    #: The cumulative figure recovered, not the amount of this payment.
    recovered_minor: int | None = Field(default=None, ge=0)
    prospects: float | None = Field(default=None, ge=0.0, le=1.0)
    position: str | None = Field(default=None, max_length=4000)
    limitation_at: datetime | None = None

    @model_validator(mode="after")
    def _normalise(self) -> RecoveryProgressRequest:
        object.__setattr__(self, "limitation_at", _as_utc(self.limitation_at))
        return self


class RecoveryTaskRequest(SchemaBase):
    """Something that has to happen for a recovery to progress.

    `recovery_id` is optional and the absence is real: "obtain the police report" is
    recovery work before anybody knows which recovery it will support.
    """

    label: str = Field(min_length=1, max_length=255)
    owner: str = Field(min_length=1, max_length=255)
    recovery_id: uuid.UUID | None = None
    due_at: datetime | None = None

    @field_validator("label", "owner")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("This cannot be blank.")
        return trimmed

    @model_validator(mode="after")
    def _normalise(self) -> RecoveryTaskRequest:
        object.__setattr__(self, "due_at", _as_utc(self.due_at))
        return self


class RecoveryTaskUpdateRequest(SchemaBase):
    done: bool


# ---------------------------------------------------------------------------
# Fraud and the SIU case
# ---------------------------------------------------------------------------


class SiuReferralRequest(SchemaBase):
    """Refer the claim to special investigations.

    **The reason is required.** A referral is read outside the claims desk and is an
    accusation of sorts against the insured; one with no recorded rationale is the
    write on this API a regulator would ask about first.
    """

    reason: str = Field(min_length=1, max_length=4000)
    investigator: str | None = Field(default=None, max_length=255)
    siu_reference: str | None = Field(default=None, max_length=64)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Say why you are referring it.")
        return trimmed


class SiuScreenRequest(SchemaBase):
    """Record that somebody is looking, without accusing anybody yet.

    The state the tab could never reach honestly, and the difference between "the
    model scored this claim" and "a person is examining it".
    """

    note: str | None = Field(default=None, max_length=4000)


class SiuStatusRequest(SchemaBase):
    """Move the investigation.

    The two conditional requirements are enforced in the service rather than here,
    because both depend on the case's current state as well as the target: an
    investigator already named on the case satisfies the first, and this schema
    cannot see the case.
    """

    status: SiuStatus
    investigator: str | None = Field(default=None, max_length=255)
    siu_reference: str | None = Field(default=None, max_length=64)
    #: Required by the service when closing. A closed case with no recorded outcome
    #: cannot answer the only question anybody asks of it later.
    outcome: str | None = Field(default=None, max_length=4000)
    recommended_actions: list[str] | None = None


class FraudDispositionRequest(SchemaBase):
    """What a reviewer concluded about one indicator.

    `discounted` requires a note, enforced in the service: accepting an indicator
    agrees with the assessment and needs no defence, while dismissing one is a person
    overruling a fraud signal.
    """

    code: str = Field(min_length=1, max_length=64)
    disposition: FraudDisposition
    note: str | None = Field(default=None, max_length=4000)


def to_handler(handler: Any) -> HandlerOut:
    return HandlerOut(
        id=handler.id,
        full_name=handler.full_name,
        email=handler.email,
        has_account=handler.subject is not None,
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
