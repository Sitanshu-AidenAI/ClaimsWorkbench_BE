"""The mailbox ledger: what arrived, what became of it, and what did not.

This is not a second copy of the FNOL case. It is the record of *collection* —
one row per Graph message the service has ever looked at — and it exists because
the interesting failures happen before a notice exists to record them on. A
mailbox that could not be reached, an attachment that would not download, an
envelope that produced no case: none of those have an `FNOLCase` to hang off,
and all of them have to be answerable when someone asks "the broker says they
sent it — where is it?".

It also carries the idempotency guarantee. `graph_message_id` and
`internet_message_id` are both unique, because a message can be re-delivered
under a new Graph id and re-listed under the same one, and neither of those may
produce a second claim notification.

`fnol_case_id` is the seam to everything downstream: once a row reaches
`processed`, the case it produced holds the documents, and the document
ingestion and extraction pipeline works from those.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import MailAttachmentStatus, MailIntakeStatus


class MailIntakeMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One message collected from the shared mailbox."""

    __tablename__ = "mail_intake_messages"

    #: The mailbox it was read from, stored rather than assumed: the configured
    #: address changes, and a two-year-old row has to still say where it came
    #: from.
    mailbox: Mapped[str] = mapped_column(String(320), index=True)

    graph_message_id: Mapped[str] = mapped_column(String(512), unique=True)
    #: RFC 5322 `Message-ID`. Unique where present — Postgres allows any number
    #: of nulls in a unique index, which is the behaviour wanted here.
    internet_message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(512), index=True)

    subject: Mapped[str | None] = mapped_column(String(998))
    sender_name: Mapped[str | None] = mapped_column(String(255))
    sender_address: Mapped[str | None] = mapped_column(String(320), index=True)
    #: `["Claims Intake <claims@carrier.example>", ...]` — the envelope as sent.
    to_recipients: Mapped[list[str]] = mapped_column(JSONB, default=list)
    cc_recipients: Mapped[list[str]] = mapped_column(JSONB, default=list)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)

    body_preview: Mapped[str | None] = mapped_column(Text)
    #: The body as it arrived, HTML and all. The notice stores a stripped copy;
    #: this one is the original, because the original is the evidence.
    body_content: Mapped[str | None] = mapped_column(Text)
    body_content_type: Mapped[str] = mapped_column(String(16), default="text")

    #: Anything else off the envelope worth keeping but not worth a column.
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    status: Mapped[str] = mapped_column(String(16), default=MailIntakeStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: What intake did to the mailbox copy, recorded so a re-run knows whether
    #: the flag it set actually landed.
    marked_read: Mapped[bool] = mapped_column(Boolean, default=False)
    moved_to_folder: Mapped[str | None] = mapped_column(String(255))

    fnol_case_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="SET NULL"), index=True
    )

    attachments: Mapped[list[MailIntakeAttachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="mail_attempts_non_negative"),
        Index("ix_mail_intake_messages_status_received_at", "status", "received_at"),
    )


class MailIntakeAttachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One attachment on one collected message, stored or refused.

    A row exists whatever the outcome. The refusals are the point: an officer
    told "the survey report is not on the notice" needs the answer to be "it was
    62MB and the limit is 25MB", not silence.

    Where the bytes were kept, `fnol_document_id` points at the `FNOLDocument`
    that owns them — the same row the extraction pipeline will read. This table
    never becomes a second document store; it records provenance.
    """

    __tablename__ = "mail_intake_attachments"

    mail_intake_message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("mail_intake_messages.id", ondelete="CASCADE"),
        index=True,
    )
    graph_attachment_id: Mapped[str] = mapped_column(String(512))

    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(128))
    #: What Graph declared, which is what the size decision was taken on.
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False)

    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(16), default=MailAttachmentStatus.STORED)
    #: Why it was skipped or how it failed — one sentence, fit to show a human.
    detail: Mapped[str | None] = mapped_column(Text)

    fnol_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_documents.id", ondelete="SET NULL")
    )

    message: Mapped[MailIntakeMessage] = relationship(back_populates="attachments")

    __table_args__ = (
        UniqueConstraint(
            "mail_intake_message_id",
            "graph_attachment_id",
            name="uq_mail_intake_attachment",
        ),
        CheckConstraint("size_bytes >= 0", name="mail_attachment_size_non_negative"),
    )


class MailIntakeRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One poll of the mailbox: when it ran, what it found, and whether it worked.

    This table exists because of a failure that has no other trace. Every safety
    net in `app.services.mail.intake` — the dropped-message reconciliation, the
    blind-sweep check, the folder-against-ledger count — is computed *inside* a
    poll, so all of them are silent in the one case that matters most: no poll
    ran at all. A dead scheduler and an empty mailbox produce identical evidence
    (no rows, no errors, a healthy API), and the difference has twice been found
    only by someone noticing that a broker's email never arrived — once six days
    later.

    A row per attempt, written whether the attempt succeeded or not, makes "when
    did intake last run" a question the database answers. That is what lets a
    process which is *not* the poller — the API, which is always up — notice the
    poller is gone. A detector that runs inside the thing it watches cannot
    report the thing being dead.

    Deliberately append-only and deliberately cheap: one small row per poll. At
    the aggressive end of the configured interval that is a few thousand rows a
    day, which is why `prune` exists on the repository and why nothing here
    stores a message body.
    """

    __tablename__ = "mail_intake_runs"

    mailbox: Mapped[str] = mapped_column(String(320), index=True)
    #: Who asked: `schedule` (Celery beat), `manual` (the trigger endpoint) or
    #: `cli` (`python -m app.services.mail`). Kept because a mailbox that only
    #: ever moves when a human presses the button is exactly the condition this
    #: table was added to make visible.
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The watermark this poll swept forward from. `None` means the whole folder
    #: was read, which is what an empty ledger asks for.
    swept_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fetched: Mapped[int] = mapped_column(Integer, default=0)
    ingested: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    abandoned: Mapped[int] = mapped_column(Integer, default=0)
    #: Messages listed that left no ledger row — a silent loss. See the intake
    #: service's `_reconcile`.
    dropped: Mapped[int] = mapped_column(Integer, default=0)

    folder_total: Mapped[int | None] = mapped_column(Integer)
    folder_unread: Mapped[int | None] = mapped_column(Integer)
    ledger_total: Mapped[int | None] = mapped_column(Integer)
    sweep_blind: Mapped[bool] = mapped_column(Boolean, default=False)

    #: False when the mailbox could not be listed at all. A poll that listed the
    #: folder and had individual messages fail is `ok`: those failures are rows
    #: in `mail_intake_messages` with their own retry count.
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # The health check's only query: the newest run for one mailbox.
        Index("ix_mail_intake_runs_mailbox_started_at", "mailbox", started_at.desc()),
        CheckConstraint("fetched >= 0", name="mail_run_fetched_non_negative"),
    )


__all__ = ["MailIntakeAttachment", "MailIntakeMessage", "MailIntakeRun"]
