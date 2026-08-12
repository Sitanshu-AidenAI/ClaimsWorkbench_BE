"""The claim a notice becomes, plus its triage and its assignment.

The claim carries a copy of the loss facts rather than reading them through the
FNOL. That is deliberate duplication: a claim is the record of what was agreed at
the point of creation, and an officer correcting a typo on the notice two weeks
later must not silently rewrite the claim's own history. The link back to the
FNOL is kept for everything else — documents, intelligence, audit trail.
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
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import AssignmentStatus, ClaimStatus, Priority


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

    created_by: Mapped[str | None] = mapped_column(String(255))
    handler_name: Mapped[str | None] = mapped_column(String(255))

    triage: Mapped[ClaimTriage | None] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    assignment: Mapped[ClaimAssignment | None] = relationship(
        back_populates="claim", cascade="all, delete-orphan", uselist=False, lazy="selectin"
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


__all__ = ["Claim", "ClaimAssignment", "ClaimTriage"]
