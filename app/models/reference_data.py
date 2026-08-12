"""Reference data the FNOL pipeline matches against.

Policies, catastrophe events and claims handlers are all *someone else's* record
in a mature estate: policy administration, a CAT data provider, and the HR or
identity system respectively. They are modelled here as local tables with an
explicit `external_id` so each can later be fed by a synchronising integration
without the matching services changing at all — they read repositories, and a
repository can be backed by a table or by a client.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Policy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A policy the FNOL can be matched to.

    Deliberately flat and denormalised: this is a read-side projection for
    matching, not the policy administration system's own model. Perils, exclusions
    and endorsements are JSON because their shape differs per line of business and
    nothing in this service reasons about their internals beyond keyword presence.
    """

    __tablename__ = "policies"

    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    policy_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    insured_name: Mapped[str] = mapped_column(String(255), index=True)
    insured_organisation: Mapped[str | None] = mapped_column(String(255))
    insured_email: Mapped[str | None] = mapped_column(String(255))
    insured_phone: Mapped[str | None] = mapped_column(String(64))

    broker_name: Mapped[str | None] = mapped_column(String(255))
    broker_reference: Mapped[str | None] = mapped_column(String(128))

    policy_type: Mapped[str | None] = mapped_column(String(64))
    line_of_business: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")

    effective_date: Mapped[date] = mapped_column(Date)
    expiry_date: Mapped[date] = mapped_column(Date)

    country: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(96))
    primary_location: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6, asdecimal=False))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6, asdecimal=False))

    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    limit_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    deductible_amount_minor: Mapped[int | None] = mapped_column(BigInteger)

    perils_covered: Mapped[list[str]] = mapped_column(JSONB, default=list)
    exclusions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    endorsements: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    locations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    __table_args__ = (
        CheckConstraint("expiry_date >= effective_date", name="policy_period_ordered"),
        Index("ix_policies_insured_name_lower", "insured_name"),
    )


class CatEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A catastrophe or major event a loss can be attributed to.

    Seeded locally today; the `external_id` and `provider` columns are the seam a
    PERILS or NOAA feed lands on. Matching is by place, date and peril rather than
    by anything the provider decides, so swapping the source does not change how a
    claim is attributed.
    """

    __tablename__ = "cat_events"

    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(64), default="internal")
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    name: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    perils: Mapped[list[str]] = mapped_column(JSONB, default=list)
    severity: Mapped[str | None] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="open")

    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date)

    country: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(96))
    affected_areas: Mapped[list[str]] = mapped_column(JSONB, default=list)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6, asdecimal=False))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6, asdecimal=False))
    radius_km: Mapped[float | None] = mapped_column(Numeric(8, 2, asdecimal=False))

    __table_args__ = (CheckConstraint("end_date >= start_date", name="cat_event_window_ordered"),)


class Handler(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Someone a claim can be assigned to.

    `subject` is the identity provider's `sub`, so a handler row and a signed-in
    principal can be reconciled without matching on email — which changes.
    Workload is a stored counter maintained by the assignment service rather than a
    live count, so recommending a handler does not cost a scan of the claims table.
    """

    __tablename__ = "handlers"

    subject: Mapped[str | None] = mapped_column(String(128), unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True)

    team: Mapped[str] = mapped_column(String(96), index=True)
    job_title: Mapped[str | None] = mapped_column(String(96))
    skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    lines_of_business: Mapped[list[str]] = mapped_column(JSONB, default=list)
    countries: Mapped[list[str]] = mapped_column(JSONB, default=list)

    #: The highest severity this handler is authorised to own.
    max_severity: Mapped[str] = mapped_column(String(16), default="medium")
    authority_limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")

    open_claims: Mapped[int] = mapped_column(Integer, default=0)
    capacity: Mapped[int] = mapped_column(Integer, default=25)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ReferenceSequence(Base, TimestampMixin):
    """Per-prefix, per-year allocation of human references.

    A table rather than a Postgres sequence because the number resets each year
    and is scoped by prefix; `SELECT ... FOR UPDATE` on one row is both cheaper and
    more obvious than a sequence per prefix-year that nothing ever drops.
    """

    __tablename__ = "reference_sequences"

    prefix: Mapped[str] = mapped_column(String(8), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, default=0)


__all__ = ["CatEvent", "Handler", "Policy", "ReferenceSequence"]
