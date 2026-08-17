"""Request and response shapes for mailbox intake.

Mapped explicitly, like the FNOL schemas: the ledger holds the body of every
message a broker has ever sent the claims desk, and none of that should reach a
client because a column was added. What is published is the envelope, the
outcome and the notice it became.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage
from app.schemas.common import SchemaBase
from app.services.mail.intake import MailIntakeSummary


class MailIntakeRunResult(SchemaBase):
    """What one poll did."""

    mailbox: str
    fetched: int
    ingested: int
    duplicates: int
    failed: int
    abandoned: int
    attachments_stored: int
    attachments_skipped: int
    attachments_failed: int
    #: The FNOL references this poll produced or matched, in collection order.
    references: list[str]


class MailIntakeAttachmentSummary(SchemaBase):
    id: uuid.UUID
    filename: str
    content_type: str | None
    size_bytes: int
    status: str
    detail: str | None
    checksum_sha256: str | None
    fnol_document_id: uuid.UUID | None


class MailIntakeMessageSummary(SchemaBase):
    id: uuid.UUID
    mailbox: str
    graph_message_id: str
    internet_message_id: str | None
    subject: str | None
    sender_name: str | None
    sender_address: str | None
    received_at: datetime
    status: str
    attempts: int
    last_error: str | None
    processed_at: datetime | None
    marked_read: bool
    moved_to_folder: str | None
    fnol_case_id: uuid.UUID | None
    attachment_count: int
    attachments: list[MailIntakeAttachmentSummary]


class MailIntakeListResult(SchemaBase):
    items: list[MailIntakeMessageSummary]
    total: int
    page: int
    page_size: int
    #: Ledger-wide counts, so the page does not have to add up what it can see.
    status_counts: dict[str, int]


def to_run_result(summary: MailIntakeSummary) -> MailIntakeRunResult:
    return MailIntakeRunResult(
        mailbox=summary.mailbox,
        fetched=summary.fetched,
        ingested=summary.ingested,
        duplicates=summary.duplicates,
        failed=summary.failed,
        abandoned=summary.abandoned,
        attachments_stored=summary.attachments_stored,
        attachments_skipped=summary.attachments_skipped,
        attachments_failed=summary.attachments_failed,
        references=list(summary.references),
    )


def to_attachment_summary(row: MailIntakeAttachment) -> MailIntakeAttachmentSummary:
    return MailIntakeAttachmentSummary(
        id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        status=row.status,
        detail=row.detail,
        checksum_sha256=row.checksum_sha256,
        fnol_document_id=row.fnol_document_id,
    )


def to_message_summary(row: MailIntakeMessage) -> MailIntakeMessageSummary:
    return MailIntakeMessageSummary(
        id=row.id,
        mailbox=row.mailbox,
        graph_message_id=row.graph_message_id,
        internet_message_id=row.internet_message_id,
        subject=row.subject,
        sender_name=row.sender_name,
        sender_address=row.sender_address,
        received_at=row.received_at,
        status=row.status,
        attempts=row.attempts,
        last_error=row.last_error,
        processed_at=row.processed_at,
        marked_read=row.marked_read,
        moved_to_folder=row.moved_to_folder,
        fnol_case_id=row.fnol_case_id,
        attachment_count=row.attachment_count,
        attachments=[to_attachment_summary(item) for item in row.attachments],
    )
