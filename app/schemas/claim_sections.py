"""The claim workbench's operational sections.

`claims.py` holds what a document-processing API owns — extraction,
classification, confidence, rules. This holds what a *claims* system owns around
it: the visit that was commissioned, the assessment a handler wrote, the money on
the file, the SIU position, what can be recovered, and the record of who did what.
Two files for the reason the frontend splits `workbench.ts` from
`workbenchSections.ts`: they are two endpoints, and the workbench aggregate is
already the largest payload on the desk.

**One of the six sections is unbuilt, and it says so rather than inventing
figures.** There is no recovery model on the server, so that section arrives empty
with `available=False` and a sentence saying why. The alternative —
fixture-shaped defaults — is the failure this codebase avoids everywhere else: a
zero that means "not tracked" is worse on a financial screen than an absent figure,
and a fabricated loss adjuster is worse than either.

Two sections used to be down there with it. The **assessment** left when coverage
became real — sections, the parties on them and the excess behind them, which is
CLAWS entry categories 4, 5, 6 and 7. What it still lacks is a priced breakdown of
the damage and a record of what the handler is waiting on, so `damage_heads`,
`factors` and `open_questions` stay empty and the section stays honest about it.

The **field inspection** left when `claim_inspections` arrived, and it left
differently: it now reports `available=True` on every claim, including one nobody
has sent an adjuster to. *Not commissioned* is a state this system holds rather
than a gap in it, which is what lets the tab lead with an action instead of an
apology.

`available` is not "did the query succeed". It is "does this product model this
yet", which is a fact the screen needs in order to tell *empty* apart from
*unbuilt*. A recoveries tab saying "nothing to recover" and one saying "recoveries
are not tracked here" are different statements, and only the second is true today.
`unavailable_reason` carries the sentence, so the screen states it in the server's
words rather than hard-coding an apology that goes stale the day the section becomes
real.

**Every list is fully typed even where it is always empty today.** Describing the
element shape now is what lets the service populate it later without a schema
change and without a frontend that has typed against `unknown[]` in the meantime.
The shapes mirror `ClaimsWorkbench_FE/src/types/workbenchSections.ts`, which is
where they were designed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas.common import SchemaBase
from app.schemas.fnol import Money


class SectionNoteOut(SchemaBase):
    """A note written by a person, attributed and timed."""

    id: uuid.UUID
    author: str
    written_at: datetime
    body: str


# ---------------------------------------------------------------------------
# Field inspection
# ---------------------------------------------------------------------------


class DamageObservationOut(SchemaBase):
    """One thing the adjuster looked at, and what they found."""

    id: uuid.UUID
    element: str
    severity: str
    finding: str
    quantified: Money | None
    photo_count: int


class InspectionSiteOut(SchemaBase):
    """The site, as the adjuster was given it.

    All of it nullable but the address, and the address only nearly. A desk
    commissioning a visit knows where the loss happened, because the claim does;
    it does not necessarily know the unit number, who has the keys or what the
    gate code is. Those arrive when somebody finds them out, and until then they
    are absent rather than blank — the screen shows a dash, which is true, instead
    of an empty string that reads as an answer.
    """

    kind: str | None
    address: str | None
    identifier: str | None
    contact_name: str | None
    contact_phone: str | None
    access_note: str | None


class InspectionEvidenceOut(SchemaBase):
    """What was collected on the visit, counted. The files themselves are elsewhere."""

    photographs: int
    measurements: int
    statements: int
    documents: int


class InspectionActionOut(SchemaBase):
    """Something that has to happen next, and who owns it."""

    id: uuid.UUID
    label: str
    owner: str
    due_at: datetime | None
    done: bool


class ClaimInspectionOut(SchemaBase):
    """Where a commissioned visit has got to.

    Real, and the newest of the six sections. `available` is now true on every
    claim: a visit is either commissioned or it is not, and *not commissioned* is a
    state this system holds rather than a gap in it. `unavailable_reason` therefore
    stays null, and the field stays on the schema because the contract is shared
    with the sections that are still honestly unbuilt.

    `status` is `not_commissioned` until somebody instructs a visit. That is the
    only value the claim carries with no `claim_inspections` row behind it, and
    everything below it is null or empty in that state — which is what the screen
    needs in order to lead with *Commission a visit* rather than with six dashes.

    `next_statuses` is the state machine, sent rather than reimplemented. The
    screen has to know that a booked visit can be attended or moved and that a
    completed one cannot be un-completed, and a second copy of
    `app.domain.inspection._ALLOWED` in TypeScript would drift from this one within
    a release.
    """

    available: bool
    unavailable_reason: str | None
    #: Where it may go from `status`. Empty means nowhere: the inspection is done.
    next_statuses: list[str]
    status: str
    reference: str | None
    adjuster_name: str | None
    adjuster_firm: str | None
    commissioned_by: str | None
    commissioned_at: datetime | None
    scheduled_at: datetime | None
    attended_at: datetime | None
    report_due_at: datetime | None
    site: InspectionSiteOut | None
    summary: str | None
    observations: list[DamageObservationOut]
    evidence: InspectionEvidenceOut
    actions: list[InspectionActionOut]
    notes: list[SectionNoteOut]


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


class CoverageSectionOut(SchemaBase):
    """One section of the policy, weighed against what is being claimed under it.

    `id` is the section key — derived from the peril's name, stable across
    re-proposals, and what every write addresses the section by. Not a row id: a
    caller holding an id would break the moment the sections were re-proposed.

    The four override fields are the pattern the rest of the product uses for an
    AI conclusion a person can change. `proposed_standpoint` is what the rules
    concluded and `standpoint` is where the section stands, so a handler reading
    the tab can see which positions are theirs and which are the machine's — and
    an auditor can see which were departed from.
    """

    id: str
    label: str
    standpoint: str
    limit: Money | None
    #: Set by a handler only. The policy book carries no schedule, so a sublimit
    #: cannot be proposed — see `app.domain.coverage.propose_sections`.
    sublimit: Money | None
    claimed: Money | None
    note: str
    #: What the rules proposed. Equal to `standpoint` until somebody departs from it.
    proposed_standpoint: str | None
    #: Which check in the stored coverage analysis drove the proposal.
    source_check: str | None
    overridden: bool
    overridden_by: str | None
    override_reason: str | None
    #: Null until a person has taken a position. The row existing is not a position.
    confirmed_by: str | None
    confirmed_at: datetime | None
    #: The parties this section concerns. CLAWS entry category 6.
    parties: list[CoverageSectionPartyOut]


class CoverageSectionPartyOut(SchemaBase):
    """A party on one section, as the section reads them.

    Denormalised onto the section rather than left as an id for the client to join:
    the tab draws the names under each section, and a client-side join across two
    lists is how a name ends up under the wrong heading.
    """

    party_id: uuid.UUID
    name: str
    role: str
    basis: str | None


class LiabilityOut(SchemaBase):
    """Who carries the loss. The handler's position, not the adjuster's finding."""

    position: str
    insured_share: float | None
    rationale: str
    third_party: str | None


class DamageHeadOut(SchemaBase):
    """One line of the damage the handler has accepted, priced."""

    id: str
    label: str
    claimed: Money
    assessed: Money
    basis: str


class DecisionFactorOut(SchemaBase):
    """Something that pushed the assessment where it went."""

    id: str
    label: str
    detail: str
    direction: str
    source: str | None


class OpenQuestionOut(SchemaBase):
    """Something the handler still needs before the file can be decided."""

    id: str
    question: str
    awaiting: str
    raised_at: datetime
    blocking: bool


class ClaimPartyOut(SchemaBase):
    """Someone on the claim.

    `source` distinguishes a party the extraction read from one a handler typed,
    which is the distinction criterion 9 audits and the BRD requires. `screened`
    is deliberately absent: sanctions screening does not exist, and a field
    reporting it would be the one lie on this payload.
    """

    id: uuid.UUID
    role: str
    name: str
    organisation: str | None
    email: str | None
    phone: str | None
    address: str | None
    notes: str | None
    is_primary: bool
    source: str
    confidence: float | None
    #: The notice row this was copied from, where it came from one. Null for a
    #: party added on the claim after the notification closed.
    fnol_party_id: uuid.UUID | None


class ClaimAssessmentOut(SchemaBase):
    """The handler's position on the claim.

    Partly real, and the split is worth stating precisely. `severity` is the
    claim's own graded severity. `recommendation` is *derived from where the claim
    actually got to* rather than guessed — an approved claim recommends settling
    because it was settled, and a claim nobody has touched recommends holding.
    The notes are the handler's own.

    `coverage_sections` is empty because there is no `Coverage` entity to select
    from. That is not a small omission and it should not read as one: coverage
    *assessment* produces a verdict, which the workbench aggregate already carries,
    and that is a different thing from the sections of a policy weighed one at a
    time. `damage_heads`, `factors` and `open_questions` are empty for the same
    class of reason — nothing prices a loss head by head, or records what a handler
    is waiting on, yet.
    """

    available: bool
    unavailable_reason: str | None
    assessed_by: str | None
    assessed_at: datetime | None
    severity: str
    coverage_sections: list[CoverageSectionOut]
    liability: LiabilityOut
    damage_heads: list[DamageHeadOut]
    recommendation: str
    recommended_settlement: Money | None
    recommendation_rationale: str
    factors: list[DecisionFactorOut]
    open_questions: list[OpenQuestionOut]
    notes: list[SectionNoteOut]
    #: Everyone on the claim, so a section's party links can be edited without a
    #: second read. CLAWS entry category 5 — captured here, screened nowhere yet.
    parties: list[ClaimPartyOut]
    #: What the responding sections are expected to cost, capped at their limits.
    #: Excluded and in-question sections contribute nothing — see
    #: `app.domain.coverage.exposure_minor` for why in-question is not a maximum.
    exposure: Money
    #: Sections where what is claimed exceeds what the section will pay. Surfaced
    #: rather than silently capped: it is a conversation with the insured.
    over_limit_sections: list[str]


# ---------------------------------------------------------------------------
# Financials
# ---------------------------------------------------------------------------


class ReserveLineOut(SchemaBase):
    """What is held on one movement type, and what was held before the last move.

    `previous` is stated rather than left to the screen to work out, because the
    subtraction needs the whole movement series and the screen only receives the
    latest line. Both figures come from `app.domain.claim_lifecycle`, which is the
    definition of the held figure rather than a second opinion about it.
    """

    head: str
    held: Money
    previous: Money
    set_by: str
    set_at: datetime
    rationale: str


class FinancialTransactionOut(SchemaBase):
    """One line of the claim's financial history.

    Every row today is a reserve movement — `kind` is `reserve` throughout, because
    payments, expenses and standalone recoveries are not modelled. The field is
    still a discriminator and not a constant: the screen renders five kinds, and
    the day payments land this shape does not change.

    `amount` is signed the way the ledger is: a movement is the delta rather than
    the new figure, which is what lets a total be a sum rather than a special case
    per row.
    """

    id: uuid.UUID
    kind: str
    head: str
    description: str
    counterparty: str | None
    amount: Money
    status: str
    occurred_at: datetime
    authorised_by: str | None
    reference: str | None


class ClaimDeductibleOut(SchemaBase):
    """One excess, its type, and how much of it is left.

    `remaining` and `exhausted` are computed from
    `app.domain.coverage.erosion` rather than stored, and for an aggregate they
    account for what *other* claims on the same policy have taken. That is why
    `applied_elsewhere` is stated separately: a handler seeing £4,000 left of a
    £25,000 aggregate needs to know three other losses ate the rest, not be left
    with a figure they cannot account for.

    For every non-aggregate type `applied_elsewhere` is zero by definition — a
    per-claim excess starts whole every time — and the field is present rather
    than omitted so the tab does not have to know which types erode.
    """

    id: uuid.UUID
    #: Null where the excess applies to the whole claim rather than one section.
    section_key: str | None
    deductible_type: str
    amount: Money
    #: CLAWS's cap on the deduction, which is not the same as the excess itself.
    maximum_applied: Money | None
    applied: Money
    applied_elsewhere: Money
    remaining: Money
    exhausted: bool
    comment: str | None
    set_by: str


class ClaimFinancialsOut(SchemaBase):
    """The money on the file.

    `currency` is the claim's own, and it is on the section rather than left to be
    read off one of the figures below it. The screen needs a currency to total in
    even when the deductible and the authority limit are both unknown, and reading
    it off a nullable `Money` is how a totals row ends up denominated in whichever
    field happened to be populated.

    `deductible` and `authority_limit` are nullable and often null: the first comes
    from the matched policy and a notice that matched nothing has none; the second
    belongs to an assigned handler and an unassigned claim has nobody to be
    limited. Both are stated absent rather than zeroed — a zero excess and an
    unknown excess are opposite facts, and only one of them means "the customer
    pays nothing".
    """

    available: bool
    unavailable_reason: str | None
    currency: str
    deductible: Money | None
    deductible_applied: bool
    #: Every excess on the claim, the contract-level one first. The single
    #: `deductible` above is the contract-level figure, kept because the tab's
    #: headline reads one number; this is the detail behind it, including the type
    #: CLAWS requires and — for an aggregate — what other claims have eroded.
    deductibles: list[ClaimDeductibleOut]
    reserves: list[ReserveLineOut]
    transactions: list[FinancialTransactionOut]
    estimate_total: Money | None
    estimate_source: str | None
    authority_limit: Money | None


# ---------------------------------------------------------------------------
# Fraud & SIU
# ---------------------------------------------------------------------------


class DisposedIndicatorOut(SchemaBase):
    """One fraud indicator, and what the desk concluded about it.

    The four review fields were null for as long as dispositions were held on the
    client, which is why they were dropped from the wire entirely: a handler who
    worked through eight indicators lost all eight by reloading, and the tab said so.
    They are `claim_fraud_dispositions` rows now.

    **All four null together means nobody has looked at it.** That absence is a
    state, which is why `FraudDisposition` has two values and no `undecided` — see
    `app.domain.siu`.
    """

    code: str
    title: str
    detail: str
    weight: float
    #: `accepted` or `discounted`. Null while nobody has decided.
    disposition: str | None
    disposition_note: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None


class InconsistencyOut(SchemaBase):
    """Two records on the file that do not agree."""

    id: str
    subject: str
    stated: str
    stated_source: str
    conflicts_with: str
    conflicts_source: str
    resolved: bool


class PriorClaimOut(SchemaBase):
    """A prior claim on the same policy, as the history read returned it."""

    id: uuid.UUID
    reference: str
    link: str
    loss_type: str
    settled_at: datetime | None
    settled_amount: Money | None
    outcome: str
    material: bool


class ClaimFraudReviewOut(SchemaBase):
    """The fraud position, assembled from what the pipeline already concluded.

    The red flags are the real indicators the assessment produced, with their
    weights, so this section cannot disagree with the score in the workbench
    header above it — they are the same records read twice rather than two
    opinions. `prior_claims` is real too: other claims on the same policy, which
    is the loss history no single claim screen can show.

    `siu_status` is a **record** now rather than a reading of `claims.fraud_flag`.
    That derivation gave the screen two of its six states and made *screening* mean
    nothing more than "the model was suspicious"; a case is opened when a person
    decides to open one, and only a person moves it.

    Each red flag carries the verdict somebody recorded against it, which is what
    made those verdicts survive a reload.

    `inconsistencies` is still empty, and that is the one gap left on this tab:
    nothing cross-checks two records against each other server-side. The document
    review tab does compare extracted values against the claim, so the ingredients
    exist — what is missing is a stored comparison rather than a screen-local one.
    """

    available: bool
    unavailable_reason: str | None
    siu_status: str
    referred_by: str | None
    referred_at: datetime | None
    investigator: str | None
    siu_reference: str | None
    #: Why it was referred, and what the investigation concluded. Both the desk's
    #: own words rather than a code.
    referral_reason: str | None
    outcome: str | None
    #: Where the case may go next, from `app.domain.siu`.
    next_statuses: list[str]
    #: Indicators nobody has decided about. The honest replacement for a conflict
    #: count that was always nought.
    undecided_indicators: int
    red_flags: list[DisposedIndicatorOut]
    inconsistencies: list[InconsistencyOut]
    prior_claims: list[PriorClaimOut]
    recommended_actions: list[str]
    notes: list[SectionNoteOut]


# ---------------------------------------------------------------------------
# Recoveries
# ---------------------------------------------------------------------------


class ResponsiblePartyOut(SchemaBase):
    """Who the money is being pursued from, and through whom.

    Everything but the name is nullable. A desk that has identified a subrogation
    against "the haulier" frequently does not yet know their insurer, and a blank
    that reads as an answer is worse than an absent line.
    """

    name: str
    role: str | None
    carrier: str | None
    carrier_reference: str | None
    contact: str | None


class RecoveryOpportunityOut(SchemaBase):
    """One route money is expected back by.

    `expected` and `recovered` are two figures and never added. The first is a
    judgement about the future and the second is a bank statement — see
    `app.domain.recovery`, which keeps them apart in the arithmetic for the same
    reason. `expected` is what is **still** outstanding, so a reader cannot sum the
    two into a number that means nothing.

    `prospects` is null when nobody has assessed it, which is a different fact from
    nought: a recovery judged hopeless and one nobody has judged look identical on a
    screen that stores 0.0 for both.
    """

    id: uuid.UUID
    kind: str
    status: str
    label: str
    #: 0..1, or null where nobody has judged it.
    prospects: float | None
    #: What is still outstanding — the expectation less what has already arrived.
    expected: Money
    recovered: Money
    party: ResponsiblePartyOut | None
    position: str | None
    opened_at: datetime
    #: When the recovery is barred. The one date here that costs money by being
    #: missed, which is why the section also carries `limitation_warnings`.
    limitation_at: datetime | None
    #: Where it may go next, from `app.domain.recovery`. Sent rather than
    #: reimplemented, for the reason the inspection's `next_statuses` is.
    next_statuses: list[str]


class RecoveryEventOut(SchemaBase):
    id: uuid.UUID
    recovery_id: uuid.UUID
    occurred_at: datetime
    description: str
    actor: str


class RecoveryTaskOut(SchemaBase):
    id: uuid.UUID
    recovery_id: uuid.UUID | None
    label: str
    owner: str
    due_at: datetime | None
    done: bool


class ClaimRecoveriesOut(SchemaBase):
    """What can be got back, and what already has been.

    Real, and the last of the six sections to become so. It reported
    `available=False` for as long as the workbench existed, because subrogation,
    salvage and reinsurance were concepts the product named and did not hold.

    Subrogation detection is called for in all four of Attachment 2's
    loss-assessment lanes — the only capability common to every one — and
    reinsurance attachment is a criterion-4 requirement at CLAWS update. What exists
    now is the register a handler works; **detecting** an opportunity automatically
    is still ahead, and `no_recovery_reason` stays null rather than asserting that a
    claim has nothing worth pursuing.
    """

    available: bool
    unavailable_reason: str | None
    opportunities: list[RecoveryOpportunityOut]
    events: list[RecoveryEventOut]
    tasks: list[RecoveryTaskOut]
    no_recovery_reason: str | None
    #: What is still expected back across the register, and what has arrived. Both
    #: null when the register is empty or its rows disagree on a currency — a total
    #: labelled with money it is not in is worse than no total.
    expected_total: Money | None
    recovered_total: Money | None
    #: Recoveries whose limitation date is close or gone, as sentences. The one
    #: thing on this tab that costs real money by being missed.
    limitation_warnings: list[str]
    #: `NoteSection.RECOVERY` is a writable section, so it needs somewhere to be
    #: read back. Without this a note filed against recoveries was stored, audited
    #: and never shown — a write path to a place nothing reads.
    notes: list[SectionNoteOut]


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


class ClaimActivityEventOut(SchemaBase):
    """One thing that happened, as the audit trail recorded it.

    This section is entirely real, and it is the only one that is. It is the trail
    the product has been writing all along — 28 typed event types across the notice
    and the claim — grouped by which part of the claim each event belongs to.
    Nothing new is stored to produce it, which is why it was the cheapest section
    to make true.
    """

    id: uuid.UUID
    occurred_at: datetime
    category: str
    actor: str
    actor_kind: str
    summary: str
    detail: str | None
    subject: str | None
    material: bool


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------


class ClaimSections(SchemaBase):
    """Everything the six operational tabs read for one claim.

    One payload rather than six, for the reason the workbench aggregate is one: the
    assessment cites the inspection, the financials follow the assessment and the
    activity log narrates both. Six endpoints would let them drift apart between
    the request that fetched one and the request that fetched the next.
    """

    reference: str
    inspection: ClaimInspectionOut
    assessment: ClaimAssessmentOut
    financials: ClaimFinancialsOut
    fraud: ClaimFraudReviewOut
    recoveries: ClaimRecoveriesOut
    activity: list[ClaimActivityEventOut]


__all__ = [
    "ClaimActivityEventOut",
    "ClaimAssessmentOut",
    "ClaimDeductibleOut",
    "ClaimFinancialsOut",
    "ClaimFraudReviewOut",
    "ClaimInspectionOut",
    "ClaimPartyOut",
    "ClaimRecoveriesOut",
    "ClaimSections",
    "CoverageSectionOut",
    "CoverageSectionPartyOut",
    "DamageHeadOut",
    "DamageObservationOut",
    "DecisionFactorOut",
    "DisposedIndicatorOut",
    "FinancialTransactionOut",
    "InconsistencyOut",
    "InspectionActionOut",
    "InspectionEvidenceOut",
    "InspectionSiteOut",
    "LiabilityOut",
    "OpenQuestionOut",
    "PriorClaimOut",
    "RecoveryEventOut",
    "RecoveryOpportunityOut",
    "RecoveryTaskOut",
    "ReserveLineOut",
    "ResponsiblePartyOut",
    "SectionNoteOut",
]
