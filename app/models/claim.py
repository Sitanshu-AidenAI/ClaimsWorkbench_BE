"""The claim a notice becomes, plus its triage, assignment, notes and ledger.

The claim carries a copy of the loss facts rather than reading them through the
FNOL. That is deliberate duplication: a claim is the record of what was agreed at
the point of creation, and an officer correcting a typo on the notice two weeks
later must not silently rewrite the claim's own history. The link back to the
FNOL is kept for everything else — documents, intelligence, audit trail.

`ClaimNote` and `ClaimReserveMovement` are the two things a handler can add to a
claim after it exists, and they are here rather than under `fnol.py` for the same
reason `ClaimTriage` is: they are facts about the claim, and `fnol_notes` — which
exists, and stays — is a fact about the notice. An officer's note on an incoming
notification and a handler's note on the file they are working are not the same
record with two authors; conflating them would make the intake queue's notes
appear in a settlement audit.

`ClaimCoverage`, `ClaimParty` and `ClaimDeductible` are the same argument applied
to CLAWS entry categories 4, 5, 6 and 7. `fnol_parties` holds who a broker's email
named; `claim_parties` holds who is on the claim, which is a superset that grows
after the notice is closed and carries roles no email mentions. The copy happens
once, at claim creation, and each row keeps `fnol_party_id` so its origin stays
readable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import (
    AssignmentStatus,
    ClaimStatus,
    CoverageStandpoint,
    InspectionStatus,
    NoteSection,
    Priority,
    RecoveryStatus,
    SiuStatus,
)


class Claim(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A claim on the book."""

    __tablename__ = "claims"

    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: One claim per notice. The unique constraint is the last line of defence
    #: against a retried create; the idempotency key on the FNOL is the first.
    fnol_case_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("fnol_cases.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(24), default=ClaimStatus.FNOL, index=True)
    priority: Mapped[str] = mapped_column(String(16), default=Priority.STANDARD, index=True)

    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policies.id", ondelete="SET NULL")
    )
    policy_number: Mapped[str | None] = mapped_column(String(64), index=True)
    insured_name: Mapped[str | None] = mapped_column(String(255))
    claimant_name: Mapped[str | None] = mapped_column(String(255))

    line_of_business: Mapped[str | None] = mapped_column(String(48), index=True)
    claim_type: Mapped[str | None] = mapped_column(String(64))
    loss_type: Mapped[str | None] = mapped_column(String(64))
    loss_description: Mapped[str | None] = mapped_column(Text)
    loss_location: Mapped[str | None] = mapped_column(Text)
    loss_country: Mapped[str | None] = mapped_column(String(64))

    date_of_loss: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    severity: Mapped[str | None] = mapped_column(String(16), index=True)
    reserve_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")

    cat_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cat_events.id", ondelete="SET NULL")
    )
    fraud_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    over_authority: Mapped[bool] = mapped_column(Boolean, default=False)

    #: A handler's conclusion that there is nothing to recover here, in their words.
    #:
    #: The one thing the recovery register could not express. An empty register meant
    #: two different things — nobody has looked yet, and somebody looked and found
    #: nothing — and the section had to assume the first, so every claim on the desk
    #: carried *"Recovery has not been considered yet"* whether or not it had been.
    #: A marker that is true of every claim tells a handler nothing.
    #:
    #: Held on the claim rather than as a `claim_recoveries` row because it is the
    #: absence of one: a row saying "no row" would be counted by every sum, every
    #: total and every open-pursuit tile in the section.
    #:
    #: Cleared by opening a recovery, which is the act that contradicts it — see
    #: `ClaimRecoveryService.decline`. Who concluded it and when are in the audit
    #: trail rather than in two more columns here.
    no_recovery_reason: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[str | None] = mapped_column(String(255))
    handler_name: Mapped[str | None] = mapped_column(String(255))

    triage: Mapped[ClaimTriage | None] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    assignment: Mapped[ClaimAssignment | None] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    #: Notes and movements are *not* `lazy="selectin"`, unlike the two above. The
    #: queue reads a page of claims and needs each one's triage and assignment to
    #: render a row; it needs neither its notes nor its ledger, and eagerly loading
    #: them would turn a twenty-five-row queue into two more queries per row.
    notes: Mapped[list[ClaimNote]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="raise"
    )
    movements: Mapped[list[ClaimReserveMovement]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="raise"
    )
    coverages: Mapped[list[ClaimCoverage]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="raise"
    )
    parties: Mapped[list[ClaimParty]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="raise"
    )
    deductibles: Mapped[list[ClaimDeductible]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="raise"
    )
    #: One per claim — see `ClaimInspection` on why a second concurrent expert is
    #: out of scope rather than merely unimplemented.
    inspection: Mapped[ClaimInspection | None] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False, lazy="raise"
    )
    recoveries: Mapped[list[ClaimRecovery]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="raise"
    )
    #: One per claim. A second suspicion about the same claim is the same case
    #: reopened, not a new one — see `ClaimSiuCase`.
    siu_case: Mapped[ClaimSiuCase | None] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False, lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("reserve_minor >= 0", name="reserve_non_negative"),
        CheckConstraint("paid_minor >= 0", name="paid_non_negative"),
        Index("ix_claims_status_reported_at", "status", "reported_at"),
    )


class ClaimTriage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What triage decided, and what a human did about it."""

    __tablename__ = "claim_triage"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), unique=True, index=True
    )
    #: A claim can be several things at once — a major loss that is also a CAT
    #: claim with litigation risk — so this is a list rather than a column.
    categories: Mapped[list[str]] = mapped_column(JSONB, default=list)
    recommended_route: Mapped[str] = mapped_column(String(96))
    recommended_route_key: Mapped[str] = mapped_column(String(48))
    recommended_priority: Mapped[str] = mapped_column(String(16), default=Priority.STANDARD)
    required_skill: Mapped[str | None] = mapped_column(String(64))
    required_team: Mapped[str | None] = mapped_column(String(96))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    reasoning: Mapped[str | None] = mapped_column(Text)
    #: `[{"factor": "...", "detail": "...", "weight": 0.4}]` — the explanation.
    factors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    overridden_by: Mapped[str | None] = mapped_column(String(255))
    override_reason: Mapped[str | None] = mapped_column(Text)
    original_route: Mapped[str | None] = mapped_column(String(96))

    claim: Mapped[Claim] = relationship(back_populates="triage")


class ClaimAssignment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Who the claim went to, or who it was recommended to.

    `RECOMMENDED` is a real state, not a placeholder: the engine proposes and the
    manager disposes, and a claim sitting on a queue with a recommendation against
    it is a normal, visible resting place.
    """

    __tablename__ = "claim_assignments"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), unique=True, index=True
    )
    handler_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("handlers.id", ondelete="SET NULL")
    )
    handler_name: Mapped[str | None] = mapped_column(String(255))
    team: Mapped[str | None] = mapped_column(String(96))
    queue: Mapped[str | None] = mapped_column(String(96))

    status: Mapped[str] = mapped_column(String(16), default=AssignmentStatus.RECOMMENDED)
    strategy: Mapped[str | None] = mapped_column(String(24))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    reasoning: Mapped[str | None] = mapped_column(Text)
    #: Runners-up, so a manager can pick another without re-running the engine.
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    assigned_by: Mapped[str | None] = mapped_column(String(255))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    claim: Mapped[Claim] = relationship(back_populates="assignment")


class ClaimNote(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A note a handler left on the claim. Plain text; never rendered as markup.

    `section` says which tab it was written on — the inspection conversation with
    an adjuster, the assessment rationale, the SIU thread — and defaults to
    `GENERAL` for a note written against the claim itself. One table with a
    discriminator rather than five, because the desk reads them as one chronology
    on the activity log and as five conversations on the tabs, and the tab is a
    property of the note rather than a different kind of thing.
    """

    __tablename__ = "claim_notes"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    author: Mapped[str] = mapped_column(String(255))
    section: Mapped[str] = mapped_column(String(24), default=NoteSection.GENERAL, index=True)
    body: Mapped[str] = mapped_column(Text)

    claim: Mapped[Claim] = relationship(back_populates="notes")

    __table_args__ = (Index("ix_claim_notes_claim_created", "claim_id", "created_at"),)


class ClaimReserveMovement(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One movement on the claim's reserve, in the shape CLAWS reports it.

    **A movement is a signed delta, not a new total.** A reserve raised from
    £40,000 to £60,000 is a row of `+20,000`, and what is currently held is the
    sum of the rows. That is what makes the ledger the explanation of the figure
    rather than a log beside it: `claims.reserve_minor` is a cache of this sum,
    kept in step by the write path, and `app.domain.claim_lifecycle` holds the
    arithmetic that defines it. Storing new totals instead would make "what
    changed, and who changed it" a diff between adjacent rows — which is fine
    until two movements land in the same second.

    **The currency pair is two columns, not one.** CLAWS requires every reserve in
    both the currency it was incurred in and the currency the book is kept in, and
    a single `currency` column is the defect this pair fixes: a €200,000 reserve on
    a sterling book is two different true figures, and reporting either one alone
    is wrong. `accounting_amount_minor` is nullable because the rate that converts
    them is a fact we do not hold yet — an absent accounting figure is honest, and
    a figure converted at 1.0 is not.

    No `status` column, deliberately. A reserve movement is a book entry, and it
    takes effect when it is written; it is a *payment* that waits for approval, and
    payments are not this table. The distinction is worth keeping: a pending
    reserve would be a figure that is neither held nor not held.
    """

    __tablename__ = "claim_reserve_movements"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    movement_type: Mapped[str] = mapped_column(String(16), index=True)
    #: CLAWS's second level of classification. A free column rather than an enum
    #: because the code list lives in Attachment 1, which we do not hold — so this
    #: is where it lands when it arrives, without a migration that rewrites a type.
    sub_movement_type: Mapped[str | None] = mapped_column(String(64))

    #: Signed. Positive raises what is held, negative releases it.
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    accounting_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    accounting_currency: Mapped[str | None] = mapped_column(String(3))

    #: Why the figure moved. Required rather than nullable: an unexplained reserve
    #: movement is the single thing an auditor is most reliably going to ask about,
    #: and the cheapest moment to capture the answer is the moment it is made.
    rationale: Mapped[str] = mapped_column(Text)
    #: What the figure was set against — the adjuster's schedule, a contractor's
    #: quote, an invoice number.
    basis: Mapped[str | None] = mapped_column(String(255))

    set_by: Mapped[str] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    claim: Mapped[Claim] = relationship(back_populates="movements")

    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="movement_non_zero"),
        CheckConstraint(
            "(accounting_amount_minor IS NULL) = (accounting_currency IS NULL)",
            name="accounting_pair_complete",
        ),
        Index("ix_claim_movements_claim_occurred", "claim_id", "occurred_at"),
    )


class ClaimCoverage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One section of the policy, and where it stands against this loss.

    CLAWS entry category 4 is "select coverages", and until this table existed
    there was nothing to select: `assess_coverage` produces a *verdict* about
    whether the policy responds, which is a different question from which sections
    are being paid under. This is the answer to the second one.

    **`section_key` is unique per claim, and it is derived rather than generated.**
    It comes from the peril's own name, so re-proposing against the same policy
    lands on the same rows and a handler's override survives it. A generated id
    would make every re-proposal a fresh set of sections and silently discard
    every position anybody had taken.

    **`proposed_standpoint` is kept beside `standpoint`.** The first is what the
    rules concluded, the second is where the section actually stands, and keeping
    both is what makes an override legible as an override rather than becoming the
    truth — the same pattern `ClaimTriage.original_route` uses, and the same one
    the BRD's audit requirements ask for.

    `sublimit_minor` is nullable and will usually be null. The policy book carries
    one overall limit and no schedule, so a sublimit is a handler's edit until
    Global Genius supplies a real one — see `app.domain.coverage.propose_sections`,
    which says so in the note it writes on every row.
    """

    __tablename__ = "claim_coverages"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    #: Derived from the peril name. Unique per claim — see the class docstring.
    section_key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(160))

    standpoint: Mapped[str] = mapped_column(
        String(24), default=CoverageStandpoint.IN_QUESTION, index=True
    )
    #: What the rules proposed, kept so an override stays visible as one.
    proposed_standpoint: Mapped[str | None] = mapped_column(String(24))
    #: Which check in the stored coverage analysis drove the proposal.
    source_check: Mapped[str | None] = mapped_column(String(32))

    limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    sublimit_minor: Mapped[int | None] = mapped_column(BigInteger)
    claimed_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))

    note: Mapped[str] = mapped_column(Text)

    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    overridden_by: Mapped[str | None] = mapped_column(String(255))
    override_reason: Mapped[str | None] = mapped_column(Text)
    #: Null until a person has actually taken a position on this section, which is
    #: not the same as the row existing — every row exists from claim creation.
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    claim: Mapped[Claim] = relationship(back_populates="coverages")
    party_links: Mapped[list[ClaimCoverageParty]] = relationship(
        back_populates="coverage", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("claim_id", "section_key", name="uq_claim_coverage_section"),
        CheckConstraint(
            "limit_minor IS NULL OR limit_minor >= 0", name="coverage_limit_non_negative"
        ),
        CheckConstraint(
            "sublimit_minor IS NULL OR sublimit_minor >= 0",
            name="coverage_sublimit_non_negative",
        ),
        CheckConstraint(
            "claimed_minor IS NULL OR claimed_minor >= 0", name="coverage_claimed_non_negative"
        ),
    )


class ClaimParty(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Someone on the claim: claimant, insured, broker, underwriter, adjuster.

    Copied from `fnol_parties` at claim creation and grown by hand afterwards. Two
    tables rather than one because they answer to different documents — see the
    module docstring — and because a party added three weeks into a liability claim
    has nowhere to go on a notice that is closed.

    `fnol_party_id` is the provenance link and is deliberately **not** a foreign
    key with a cascade. A notice can be deleted; the claim's record of who was
    involved must survive it, for the same reason `audit_events` carries no key to
    the case it describes.
    """

    __tablename__ = "claim_parties"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    #: The notice row this was copied from, where it was copied from one. Not a
    #: foreign key — see the class docstring.
    fnol_party_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    role: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    organisation: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    #: `ai` for a party the extraction read, `human` for one a handler added. The
    #: distinction the audit trail has to be able to draw.
    source: Mapped[str] = mapped_column(String(16), default="human")
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))

    claim: Mapped[Claim] = relationship(back_populates="parties")
    coverage_links: Mapped[list[ClaimCoverageParty]] = relationship(
        back_populates="party", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (Index("ix_claim_parties_claim_role", "claim_id", "role"),)


class ClaimCoverageParty(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Which parties one coverage section concerns. CLAWS entry category 6.

    A table rather than an array column on either side, because the link carries
    facts of its own — who made it and why — and because both directions are read:
    "who is claiming under business interruption" and "which sections does this
    third party appear under" are both questions a handler asks.
    """

    __tablename__ = "claim_coverage_parties"

    coverage_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_coverages.id", ondelete="CASCADE"), index=True
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_parties.id", ondelete="CASCADE"), index=True
    )
    #: Why this party is on this section — "named insured", "injured employee".
    basis: Mapped[str | None] = mapped_column(String(160))
    linked_by: Mapped[str] = mapped_column(String(255))

    coverage: Mapped[ClaimCoverage] = relationship(back_populates="party_links")
    party: Mapped[ClaimParty] = relationship(back_populates="coverage_links")

    __table_args__ = (UniqueConstraint("coverage_id", "party_id", name="uq_claim_coverage_party"),)


class ClaimDeductible(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The excess on the claim, and how much of it has been used. Category 7.

    `coverage_id` is nullable, and the null case is the common one: most policies
    carry a single excess at contract level, and only some carry one per section.
    A null means "the whole claim", which is why it is not defaulted to a section.

    **`applied_minor` is what has actually been taken off a payment**, not what is
    due. It stays zero until a payment carries it, which is why
    `ClaimFinancialsOut.deductible_applied` reports false today: there are no
    payments yet. The arithmetic that decides what *should* come off a given
    settlement — and the franchise rule, which does not deduct at all — lives in
    `app.domain.coverage.deduction_for` rather than here.

    `maximum_applied_minor` is CLAWS's own field for a cap on the deduction, which
    is not the same as the excess itself: a policy can carry a 5% excess capped at
    a fixed figure, and storing only one of the two loses the cap.
    """

    __tablename__ = "claim_deductibles"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    #: Null means the excess applies to the whole claim rather than one section.
    coverage_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_coverages.id", ondelete="CASCADE")
    )

    deductible_type: Mapped[str] = mapped_column(String(24))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    maximum_applied_minor: Mapped[int | None] = mapped_column(BigInteger)
    #: What has been taken off a payment. Zero until one carries it.
    applied_minor: Mapped[int] = mapped_column(BigInteger, default=0)

    comment: Mapped[str | None] = mapped_column(Text)
    set_by: Mapped[str] = mapped_column(String(255))

    claim: Mapped[Claim] = relationship(back_populates="deductibles")

    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="deductible_amount_non_negative"),
        CheckConstraint("applied_minor >= 0", name="deductible_applied_non_negative"),
        CheckConstraint(
            "maximum_applied_minor IS NULL OR maximum_applied_minor >= 0",
            name="deductible_maximum_non_negative",
        ),
        Index("ix_claim_deductibles_claim_coverage", "claim_id", "coverage_id"),
    )


class ClaimInspection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The loss-adjuster visit commissioned on this claim.

    **One per claim, and that is a deliberate simplification worth stating.** A
    large loss can carry two instructions — an adjuster and then a forensic
    accountant — and this models the first as one record whose status can go round
    the `more_needed → visit_booked` loop rather than as two rows. That covers the
    second visit, which is the common case, and does not cover two concurrent
    experts, which is not. The unique constraint is what makes that limitation
    visible rather than something the desk discovers by creating a mess.

    The site fields are flattened onto the row rather than given their own table.
    They are one address with a contact, they are read as a block, and a join for
    five columns that never exist without their parent buys nothing.

    `reference` is the adjuster firm's own — `INSP-2026-004412` — and is nullable
    because it arrives with their acknowledgement rather than with the instruction.
    A visit commissioned this morning has no reference yet, and inventing one would
    put a number on the claim that no adjuster would recognise.
    """

    __tablename__ = "claim_inspections"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(24), default=InspectionStatus.TO_SCHEDULE, index=True
    )
    #: The adjuster firm's own reference. Arrives with their acknowledgement.
    reference: Mapped[str | None] = mapped_column(String(64))

    adjuster_name: Mapped[str | None] = mapped_column(String(255))
    adjuster_firm: Mapped[str | None] = mapped_column(String(255))

    #: Who the visit is *somebody's*, rather than what they are called.
    #:
    #: The name above cannot answer that. It is free text a handler typed, and it
    #: was the only link this record had — so the board could not narrow to one
    #: adjuster's own work and nothing could tell whose findings a write would be.
    #: These two are what make an inspection assignable:
    #:
    #: * `adjuster_email` is the address the instruction went to. Knowable at
    #:   commission time, and knowable *before the adjuster has an account* — which
    #:   is the ordinary case, because a firm is instructed before a person signs in.
    #: * `adjuster_subject` is their account, claimed against the email the first
    #:   time they record anything. Adoption rather than insertion, the same shape
    #:   `HandlerDirectoryService.register` uses and for the same reason: the link
    #:   then survives the address changing.
    #:
    #: Both nullable, and a null pair is not a gap to be filled in. A visit
    #: instructed to a firm with no named contact is a real state, and it means the
    #: handler records the findings — which is what happened for every inspection
    #: before this column existed.
    adjuster_subject: Mapped[str | None] = mapped_column(String(128), index=True)
    adjuster_email: Mapped[str | None] = mapped_column(String(320), index=True)

    commissioned_by: Mapped[str] = mapped_column(String(255))
    commissioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: When the visit is booked for. Null between commissioning and booking.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Null until the adjuster has actually been. Not the same as `scheduled_at`
    #: having passed — a visit can be missed, and the two must not be conflated.
    attended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When the adjuster sent their report to the handler.
    #:
    #: **Filing and accepting are two people's acts and this is the first of them.**
    #: `status = completed` is the handler accepting a report; this is the adjuster
    #: submitting one. Conflating them would make the adjuster's queue unable to
    #: tell a report still on their desk from one already with the handler, which is
    #: the single distinction that queue exists to draw.
    #:
    #: Cleared when the handler sends it back, because it is then owed again.
    filed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When a handler last returned the report asking for more.
    #:
    #: Never cleared, which is the point: it is the durable answer to "has this been
    #: sent back", and it has to survive the adjuster picking the report up again. A
    #: report that has been returned once and is now back `in_progress` is still a
    #: report that was sent back, and the queue's chip says so.
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- The site, as the adjuster was given it -----------------------------
    site_kind: Mapped[str | None] = mapped_column(String(64))
    site_address: Mapped[str | None] = mapped_column(Text)
    #: Whatever identifies the risk on site: a unit number, a VRM, a plant serial.
    site_identifier: Mapped[str | None] = mapped_column(String(128))
    site_contact_name: Mapped[str | None] = mapped_column(String(255))
    site_contact_phone: Mapped[str | None] = mapped_column(String(64))
    #: What an adjuster needs before they set off — gate codes, permits, hours.
    site_access_note: Mapped[str | None] = mapped_column(Text)

    #: The adjuster's own summary of what they found. Null before the visit.
    summary: Mapped[str | None] = mapped_column(Text)

    # --- What was collected, counted ----------------------------------------
    # Counts rather than a join to the files: the documents themselves live on the
    # notice or in FileNet, and what the tab reports is how much evidence exists.
    photographs: Mapped[int] = mapped_column(Integer, default=0)
    measurements: Mapped[int] = mapped_column(Integer, default=0)
    statements: Mapped[int] = mapped_column(Integer, default=0)
    documents: Mapped[int] = mapped_column(Integer, default=0)

    claim: Mapped[Claim] = relationship(back_populates="inspection")
    observations: Mapped[list[ClaimInspectionObservation]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", lazy="raise"
    )
    actions: Mapped[list[ClaimInspectionAction]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("photographs >= 0", name="photographs_non_negative"),
        CheckConstraint("measurements >= 0", name="measurements_non_negative"),
        CheckConstraint("statements >= 0", name="statements_non_negative"),
        CheckConstraint("documents >= 0", name="documents_non_negative"),
    )


class ClaimInspectionObservation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One thing the adjuster looked at, and what they found.

    `quantified_minor` is nullable and frequently null: an adjuster prices what
    they can price on site and leaves the rest to a contractor's quote. A null is
    "not costed", **not zero** — `app.domain.inspection.quantified_minor` sums only
    the rows that carry a figure and reports how many did, so a partial schedule
    cannot read as a complete one.

    `photo_count` is how many frames back this observation, which is what makes a
    finding checkable. An observation with no photographs is still a finding; it is
    simply one a handler has to take on the adjuster's word.
    """

    __tablename__ = "claim_inspection_observations"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_inspections.id", ondelete="CASCADE"), index=True
    )
    #: The element inspected — "Racking, bays 1-14", "Roof deck, north bay".
    element: Mapped[str] = mapped_column(String(255))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    finding: Mapped[str] = mapped_column(Text)

    quantified_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    photo_count: Mapped[int] = mapped_column(Integer, default=0)

    recorded_by: Mapped[str] = mapped_column(String(255))

    inspection: Mapped[ClaimInspection] = relationship(back_populates="observations")

    __table_args__ = (
        CheckConstraint(
            "quantified_minor IS NULL OR quantified_minor >= 0",
            name="quantified_non_negative",
        ),
        CheckConstraint("photo_count >= 0", name="photo_count_non_negative"),
        CheckConstraint(
            "(quantified_minor IS NULL) = (currency IS NULL)",
            name="money_pair_complete",
        ),
    )


class ClaimInspectionAction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Something that has to happen before the inspection is done with.

    Owned by a name rather than a foreign key to a handler: the owner is as often
    the adjuster, the insured or a contractor as it is somebody on the desk, and a
    key to `handlers` could not express three of those four.
    """

    __tablename__ = "claim_inspection_actions"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_inspections.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(255))
    owner: Mapped[str] = mapped_column(String(255))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    done_by: Mapped[str | None] = mapped_column(String(255))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raised_by: Mapped[str] = mapped_column(String(255))

    inspection: Mapped[ClaimInspection] = relationship(back_populates="actions")

    __table_args__ = (Index("ix_claim_inspection_actions_open", "inspection_id", "done"),)


class ClaimRecovery(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One route money is expected back by.

    **`expected_minor` and `recovered_minor` are two columns and never one.** The
    first is a judgement about the future, the second is a bank statement, and a
    register that stored their sum would be reporting a forecast as an asset — which
    is the specific way a recovery ledger flatters a loss ratio.
    `app.domain.recovery` keeps them apart in the arithmetic too.

    **A recovery does not net off the reserve.** `claim_lifecycle.incurred_minor`
    excludes recovery movements, and this table is deliberately not wired into the
    reserve cache: the reserve is what the claim is expected to cost, and money
    coming back is tracked against it.

    `prospects` is nullable and the null means *nobody has assessed this*, which is
    a different fact from nought. A recovery judged hopeless and one nobody has
    judged look the same on a screen that stores 0.0 for both.

    The responsible party is flattened onto the row for the reason the inspection's
    site is: it is one counterparty read as a block, and a join for five columns
    that never exist without their parent buys nothing.
    """

    __tablename__ = "claim_recoveries"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), default=RecoveryStatus.IDENTIFIED, index=True)
    #: What is being pursued, in a phrase. "Subrogation against the haulier."
    label: Mapped[str] = mapped_column(String(255))

    #: How likely the desk thinks it is, 0..1. Null means not assessed.
    prospects: Mapped[float | None] = mapped_column(Numeric(4, 3, asdecimal=False))
    expected_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    #: What has actually come back. Zero until it does, and zero is honest here —
    #: unlike `prospects`, nothing having arrived is a fact rather than a gap.
    recovered_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3))

    # --- Who the money is being pursued from --------------------------------
    party_name: Mapped[str | None] = mapped_column(String(255))
    #: `Third party`, `Contractor`, `Reinsurer`, `Salvage buyer`.
    party_role: Mapped[str | None] = mapped_column(String(64))
    party_carrier: Mapped[str | None] = mapped_column(String(255))
    party_carrier_reference: Mapped[str | None] = mapped_column(String(128))
    party_contact: Mapped[str | None] = mapped_column(String(255))

    #: Where the pursuit stands, in the handler's words.
    position: Mapped[str | None] = mapped_column(Text)
    opened_by: Mapped[str] = mapped_column(String(255))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: When the recovery must be started by, or is barred.
    #:
    #: The one date on this tab that costs money by being missed, and the reason
    #: `app.domain.recovery.limitation_warnings` exists. Deliberately not a status:
    #: a barred subrogation is still `pursuing` until somebody writes it off,
    #: because the legal position and the desk's own view are different facts.
    limitation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    claim: Mapped[Claim] = relationship(back_populates="recoveries")
    events: Mapped[list[ClaimRecoveryEvent]] = relationship(
        back_populates="recovery", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("expected_minor >= 0", name="recovery_expected_non_negative"),
        CheckConstraint("recovered_minor >= 0", name="recovery_recovered_non_negative"),
        CheckConstraint(
            "prospects IS NULL OR (prospects >= 0 AND prospects <= 1)",
            name="recovery_prospects_a_probability",
        ),
    )


class ClaimRecoveryEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One step in a recovery's history.

    Append-only in practice and not enforced as such: unlike the audit trail this is
    a working narrative a handler writes, and a mistyped entry should be correctable
    rather than permanent. The audit trail is where the immutable record lives.
    """

    __tablename__ = "claim_recovery_events"

    recovery_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_recoveries.id", ondelete="CASCADE"), index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))

    recovery: Mapped[ClaimRecovery] = relationship(back_populates="events")


class ClaimRecoveryTask(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Something that has to happen for a recovery to progress.

    `recovery_id` is **nullable**, and that is the point of the column: "obtain the
    police report" is recovery work before anybody knows which recovery it will
    support, and a task table that demanded a parent would push that into a note.
    """

    __tablename__ = "claim_recovery_tasks"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    recovery_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_recoveries.id", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(String(255))
    #: A name, not a handler key. As often the solicitor or the loss adjuster as
    #: somebody on the desk — the same reason `ClaimInspectionAction.owner` is.
    owner: Mapped[str] = mapped_column(String(255))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    done_by: Mapped[str | None] = mapped_column(String(255))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raised_by: Mapped[str] = mapped_column(String(255))

    __table_args__ = (Index("ix_claim_recovery_tasks_open", "claim_id", "done"),)


class ClaimSiuCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The special investigations case on a claim, where one has been opened.

    **One per claim, and a referral is a person's act.** `claims.fraud_flag` is the
    pipeline's own scoring; this row exists because a handler decided to refer. The
    tab used to derive its status from the flag, which gave it two of six states and
    made *screening* mean nothing more than "the model was suspicious".

    `investigator` is a name rather than a key to `handlers`: SIU is frequently a
    different company, and a foreign key could not hold one.

    `recommended_actions` is SIU's own instruction list when they write one.
    `app.domain.siu.recommended_actions` generates a fallback and defers to this,
    because an investigator's instructions beat a generated list every time.
    """

    __tablename__ = "claim_siu_cases"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(24), default=SiuStatus.SCREENING, index=True)

    referred_by: Mapped[str | None] = mapped_column(String(255))
    referred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The SIU officer who owns it. Null while a referral sits in their queue.
    investigator: Mapped[str | None] = mapped_column(String(255))
    #: The investigating unit's own reference, which arrives with their
    #: acknowledgement — the same shape as the loss adjuster's.
    siu_reference: Mapped[str | None] = mapped_column(String(64))

    #: Why it was referred, in the handler's words.
    referral_reason: Mapped[str | None] = mapped_column(Text)
    #: What the investigation concluded. Null until it closes.
    outcome: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recommended_actions: Mapped[list[str]] = mapped_column(JSONB, default=list)

    claim: Mapped[Claim] = relationship(back_populates="siu_case")


class ClaimFraudDisposition(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What a reviewer concluded about one fraud indicator.

    **This is the record that made flag dispositions survive a reload.** They were
    held in a working copy on the client, so a handler who worked through eight
    indicators lost all eight by refreshing — and the workbench said so, which was
    honest and useless.

    Keyed on the indicator's `code` rather than a row id, because the indicators are
    *derived* from the stored fraud analysis and have no rows of their own. The code
    is stable for a given rule, which is exactly what a verdict needs to be attached
    to. Unique per claim: one verdict per indicator, replaced when somebody changes
    their mind, and the audit trail keeps the previous one.

    An indicator with **no row here has not been looked at**. That absence is the
    third state, which is why `FraudDisposition` has two values and no `undecided`.
    """

    __tablename__ = "claim_fraud_dispositions"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    #: The indicator's own code — `FF-14`, `duplicate_reference`.
    code: Mapped[str] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str] = mapped_column(String(255))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Named without `op.f` so it reads the same in the model, where the service
        # relies on it to make a changed verdict an update rather than a second row.
        UniqueConstraint("claim_id", "code", name="uq_claim_fraud_disposition_code"),
    )


__all__ = [
    "Claim",
    "ClaimAssignment",
    "ClaimCoverage",
    "ClaimCoverageParty",
    "ClaimDeductible",
    "ClaimFraudDisposition",
    "ClaimInspection",
    "ClaimInspectionAction",
    "ClaimInspectionObservation",
    "ClaimNote",
    "ClaimParty",
    "ClaimRecovery",
    "ClaimRecoveryEvent",
    "ClaimRecoveryTask",
    "ClaimReserveMovement",
    "ClaimSiuCase",
    "ClaimTriage",
]
