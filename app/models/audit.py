"""The audit trail.

One append-only table across every entity rather than a history table per model.
A claims audit is read as a single narrative — "the notice arrived, the model read
it, the officer corrected the date of loss, the claim was created, it was assigned
to Rebecca" — and reconstructing that from six tables is how audit trails end up
unread.

`before` and `after` hold only the fields that moved, not whole rows: a diff is
what a reviewer needs, and a full snapshot of a claim on every edit is how this
table becomes the largest one in the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.domain.enums import ActorType


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    """One thing that happened to one entity.

    No `updated_at`: an audit row is never updated, and a column implying it could
    be would be a lie about the guarantee this table exists to make.
    """

    __tablename__ = "audit_events"

    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    #: The human reference, denormalised so the trail can be read without joins.
    entity_reference: Mapped[str | None] = mapped_column(String(32))

    event_type: Mapped[str] = mapped_column(String(48), index=True)
    summary: Mapped[str] = mapped_column(Text)

    actor: Mapped[str] = mapped_column(String(255))
    actor_type: Mapped[str] = mapped_column(String(16), default=ActorType.HUMAN)

    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64))

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (Index("ix_audit_events_entity", "entity_type", "entity_id", "occurred_at"),)


__all__ = ["AuditEvent"]
