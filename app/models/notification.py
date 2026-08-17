"""Notifications: the desk's doorbell, and who has answered it.

This is not a second audit trail. The audit trail answers "what happened to this
notice" for someone who came looking; a notification interrupts someone who did
not. The two carry different content for that reason — an audit event records a
state transition in the vocabulary of the pipeline, a notification carries the
sender, the subject and the time an email arrived, because that is what tells a
handler whether the thing that just landed is theirs.

Two tables, and the split is the point.

**`notifications` is desk-wide.** A broker's email arrives at the claims desk,
not at a person: intake runs under the service's own Graph credentials with no
human in the loop, so there is nobody to address the row to. Assigning one at
collection time would mean inventing a routing rule the business has not stated.

**`notification_reads` is per-person.** Which means one officer dismissing a
notification cannot blank the badge for the rest of the desk — the failure mode
that makes a shared inbox unusable, and the reason this is a second table rather
than a `read_at` column on the first. A row here is a fact about one subject
having seen one notification; its absence is the unread state, so nothing has to
be written when a notification is created.

`dedupe_key` is what makes emission safe to repeat. Every producer here runs on a
schedule or under a retry — a re-delivered Celery task, a mailbox re-polled after
a crash — and the same event must not become a second row on someone's panel. It
is a unique database constraint rather than a service-layer check for the same
reason `mail_intake_messages.graph_message_id` is: two workers polling one
mailbox at one moment is a race no amount of application code wins alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import NotificationTone


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One thing the claims desk is being told."""

    __tablename__ = "notifications"

    #: A `NotificationKind` value. Text with an index rather than a Postgres enum,
    #: for the reason `app.domain.enums` gives for every other status in this
    #: codebase: adding a kind should not rewrite a type other tables depend on.
    kind: Mapped[str] = mapped_column(String(64), index=True)
    tone: Mapped[str] = mapped_column(String(16), default=NotificationTone.INFO)

    #: One line, shown in bold in the panel. No trailing full stop.
    title: Mapped[str] = mapped_column(String(200))
    #: One or two sentences under it. This is the sentence a handler reads to
    #: decide whether to click, so it says what happened and what it means.
    body: Mapped[str] = mapped_column(Text)

    #: What makes emitting this event twice produce one row. Unique.
    dedupe_key: Mapped[str] = mapped_column(String(512), unique=True)

    #: When the thing being reported happened — not when the row was written.
    #: An email received at 08:02 and collected by a 08:05 poll is announced as
    #: 08:02, because that is the time the broker will quote back.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    fnol_case_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    #: Denormalised so the panel can render and link a row without joining, and
    #: so a notification still reads sensibly in a log after its case has gone.
    fnol_reference: Mapped[str | None] = mapped_column(String(32), index=True)

    mail_intake_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("mail_intake_messages.id", ondelete="SET NULL")
    )

    #: The metadata the panel shows: sender, subject, mailbox, attachment counts,
    #: the processing outcome. A column per field would be a schema change every
    #: time a new kind wants to say something, and none of it is queried on.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    reads: Mapped[list[NotificationRead]] = relationship(
        back_populates="notification", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        # The panel's only query: newest first, optionally narrowed to one case.
        Index("ix_notifications_occurred_at_desc", occurred_at.desc()),
    )


class NotificationRead(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One person has seen one notification.

    Keyed on the token subject rather than on a local user id, because there is no
    local user table — Keycloak owns identity here, and the subject is the one
    identifier that is stable across a username change.
    """

    __tablename__ = "notification_reads"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("notifications.id", ondelete="CASCADE"), index=True
    )
    #: The `sub` claim of the access token that read it.
    subject: Mapped[str] = mapped_column(String(255), index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    notification: Mapped[Notification] = relationship(back_populates="reads")

    __table_args__ = (UniqueConstraint("notification_id", "subject", name="uq_notification_read"),)


__all__ = ["Notification", "NotificationRead"]
