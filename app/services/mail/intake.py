"""Shared-mailbox intake: from a Graph message to a notice with its documents.

This service is the orchestration layer and nothing else. It does not speak
HTTP — `app.integrations.graph` does — and it does not decide what an FNOL is —
`app.services.fnol.ingestion` does. What it owns is the sequence, and the four
rules that make the sequence safe to run every five minutes forever:

* **One transaction per message.** A batch of twenty in which the eleventh
  fails must still collect the other nineteen, and the eleventh's failure must
  be *recorded*, which is impossible inside a transaction that has just been
  rolled back. So each message commits on its own, and the failure path opens a
  fresh transaction to write down what went wrong.
* **The ledger is written before the mailbox is touched.** The notice and its
  documents are committed first; only then is the message marked read. A crash
  between the two leaves a message that is collected but still unread, which the
  next poll recognises and re-marks. The other order loses claims.
* **Idempotency is checked, not assumed.** Every message is looked up on both
  its Graph id and its `Message-ID` before any work happens, and the notice
  layer checks again on its own keys. A re-delivery costs one query.
* **An attachment is never allowed to sink a message.** A file that is too
  large, of a type claims documents are not accepted in, or that simply refuses
  to download, becomes a row explaining itself. The notice is still created.

What it deliberately does *not* do is run the FNOL pipeline. The case is left
`queued`, which is the seam the document ingestion and extraction phase plugs
into: it reads cases in that state, and the documents are already stored,
checksummed and linked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import FNOLSettings, GraphSettings, settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.enums import MailAttachmentStatus, MailIntakeStatus
from app.domain.normalisation import clip
from app.integrations.graph.client import GraphMailClient
from app.integrations.graph.errors import GraphError
from app.integrations.graph.messages import GraphAttachmentMetadata, GraphMessage
from app.models.fnol import FNOLCase
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage
from app.repositories.mail_intake import MailIntakeRepository
from app.services.fnol.ingestion import FNOLIngestionService, IncomingAttachment, IncomingEmail
from app.services.fnol.service import FNOLService
from app.services.notifications.service import NotificationService

logger = get_logger(__name__)

#: How the mailbox names itself in the audit trail. Not a person, and not
#: pretending to be one: an officer reading the history of a notice should see
#: that a machine collected it.
INTAKE_ACTOR = "mailbox-intake"

#: Bound on what is kept in `last_error`. Enough for a stack-free explanation.
MAX_ERROR_CHARACTERS = 2000


@dataclass(slots=True)
class MailIntakeSummary:
    """What one poll did. Returned to the trigger and logged."""

    mailbox: str
    fetched: int = 0
    #: Messages that produced a new notice.
    ingested: int = 0
    #: Messages already collected, or whose notice already existed.
    duplicates: int = 0
    failed: int = 0
    #: Messages left alone because they have failed too many times.
    abandoned: int = 0
    attachments_stored: int = 0
    attachments_skipped: int = 0
    attachments_failed: int = 0
    references: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Downloaded:
    """An attachment that made it past every gate, with its bytes."""

    metadata: GraphAttachmentMetadata
    content: bytes


class MailIntakeService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        messages: MailIntakeRepository,
        ingestion: FNOLIngestionService,
        fnol: FNOLService,
        client: GraphMailClient,
        #: Optional so the unit tests that predate the panel can build this
        #: service without one. In every wired path it is present — see
        #: `build_mail_intake_service`.
        notifications: NotificationService | None = None,
        config: GraphSettings | None = None,
        fnol_config: FNOLSettings | None = None,
    ) -> None:
        self._session = session
        self._messages = messages
        self._ingestion = ingestion
        self._fnol = fnol
        self._client = client
        self._notifications = notifications
        self._config = config or settings.graph
        # The attachment ceiling is the FNOL module's, not a second one: the
        # limit the officer is told about on the upload screen is the limit a
        # broker's email is held to.
        self._fnol_config = fnol_config or settings.fnol

    async def poll(self, *, limit: int | None = None) -> MailIntakeSummary:
        """Collect one batch from the shared mailbox.

        Listing is the one step allowed to fail the whole poll: if the mailbox
        cannot be read there is nothing to be partial about.
        """
        summary = MailIntakeSummary(mailbox=self._client.mailbox)
        messages = await self._client.list_messages(limit=limit)
        summary.fetched = len(messages)

        for message in messages:
            try:
                await self._collect(message, summary)
            except Exception as exc:
                summary.failed += 1
                await self._record_failure(message, exc)

        logger.info(
            "mail_intake_poll_completed",
            mailbox=summary.mailbox,
            fetched=summary.fetched,
            ingested=summary.ingested,
            duplicates=summary.duplicates,
            failed=summary.failed,
            abandoned=summary.abandoned,
            attachments_stored=summary.attachments_stored,
            attachments_skipped=summary.attachments_skipped,
        )
        return summary

    # -- One message ---------------------------------------------------------

    async def _collect(self, message: GraphMessage, summary: MailIntakeSummary) -> None:
        record = await self._messages.find_existing(
            graph_message_id=message.message_id,
            internet_message_id=message.internet_message_id,
        )

        if record is not None and record.status == MailIntakeStatus.PROCESSED:
            summary.duplicates += 1
            logger.info(
                "mail_intake_message_already_collected",
                mailbox=self._client.mailbox,
                graph_message_id=message.message_id,
                fnol_case_id=str(record.fnol_case_id) if record.fnol_case_id else None,
            )
            # The flag may not have landed last time — that is why it is stored.
            await self._settle_mailbox(record, message)
            await self._session.commit()
            return

        if record is not None and record.attempts >= self._config.max_attempts:
            summary.abandoned += 1
            logger.warning(
                "mail_intake_message_abandoned",
                mailbox=self._client.mailbox,
                graph_message_id=message.message_id,
                attempts=record.attempts,
                error=record.last_error,
            )
            return

        record = self._upsert_record(record, message)
        record.status = MailIntakeStatus.PROCESSING
        record.attempts += 1
        await self._messages.flush()

        downloads = await self._download_attachments(message, record, summary)

        email = self._as_email(message, downloads)
        notification = self._ingestion.from_email(email)
        case, created = await self._ingestion.ingest(notification, actor=INTAKE_ACTOR)

        for download in downloads:
            await self._attach(case=case, download=download, record=record, summary=summary)

        if created:
            # Left queued rather than processed: the extraction pipeline is a
            # later phase, and this service's job ends with the documents stored.
            self._fnol.mark_queued(case)
            summary.ingested += 1
        else:
            summary.duplicates += 1

        record.fnol_case_id = case.id
        record.status = MailIntakeStatus.PROCESSED
        record.processed_at = datetime.now(UTC)
        record.last_error = None
        summary.references.append(case.reference)

        # Tell the desk, inside the same transaction as the notice. The ordering is
        # not incidental: a notification announcing an arrival that then failed to
        # commit would send a handler to a reference that does not exist, and this
        # is the one commit boundary that can guarantee it never happens.
        await self._announce(case=case, record=record, message=message, created=created)

        # Durable first, mailbox second. See the module docstring.
        await self._session.commit()
        await self._settle_mailbox(record, message)
        await self._session.commit()

        logger.info(
            "mail_intake_message_collected",
            mailbox=self._client.mailbox,
            graph_message_id=message.message_id,
            reference=case.reference,
            created=created,
            attachments=len(downloads),
        )

    async def _announce(
        self,
        *,
        case: FNOLCase,
        record: MailIntakeMessage,
        message: GraphMessage,
        created: bool,
    ) -> None:
        """Put this arrival on the desk's notification panel.

        Emitted for a follow-up email as well as for a new notice: a broker sending
        a survey report an hour after the notification is something the handler
        working it needs to know, and the ledger row alone does not tell them. What
        differs is the sentence — "processing has started" would be false about a
        notice that was read yesterday.
        """
        if self._notifications is None:
            return

        # Flushed first, and this is not optional: the session runs with
        # `autoflush=False`, so the attachment rows `_record_attachment` has only
        # `add`ed are invisible to the query below until they are pushed. Without
        # this the panel reported "0 attachments stored" on a notice that had just
        # stored one.
        await self._messages.flush()

        # Queried rather than read off `record.attachments`: that collection is
        # `selectin`-loaded and the record has only been flushed, so touching it
        # here would be a lazy load — synchronous IO inside async code, which
        # asyncpg refuses outright. The same reason `_record_attachment` queries.
        attachments = await self._messages.list_attachments(record.id)
        stored = sum(1 for item in attachments if item.status == MailAttachmentStatus.STORED)

        await self._notifications.fnol_email_received(
            case_id=case.id,
            reference=case.reference,
            mail_message_id=record.id,
            graph_message_id=message.message_id,
            mailbox=self._client.mailbox,
            sender_name=message.sender.name,
            sender_address=message.sender.address,
            subject=message.subject,
            received_at=message.received_at,
            attachments_stored=stored,
            attachments_skipped=len(attachments) - stored,
            created=created,
        )

    def _upsert_record(
        self, record: MailIntakeMessage | None, message: GraphMessage
    ) -> MailIntakeMessage:
        """Create the ledger row, or refresh the envelope on the existing one."""
        now = datetime.now(UTC)
        if record is None:
            # The column defaults are applied on insert, which is too late for
            # code that reads the row before it is flushed — so they are stated.
            record = MailIntakeMessage(
                mailbox=self._client.mailbox,
                graph_message_id=message.message_id,
                first_seen_at=now,
                attempts=0,
                attachment_count=0,
                marked_read=False,
                moved_to_folder=None,
            )
            self._messages.add(record)

        # Clipped to their columns. A display name long enough to overflow one is
        # not worth failing a claim notification over, and a failure at INSERT
        # would repeat on every retry.
        record.internet_message_id = clip(message.internet_message_id, 998)
        record.conversation_id = clip(message.conversation_id, 512)
        record.subject = clip(message.subject, 998)
        record.sender_name = clip(message.sender.name, 255)
        record.sender_address = clip(message.sender.address, 320)
        record.to_recipients = [item.as_text() for item in message.to_recipients]
        record.cc_recipients = [item.as_text() for item in message.cc_recipients]
        record.received_at = message.received_at
        record.has_attachments = message.has_attachments
        record.body_preview = message.body_preview
        record.body_content = message.body
        record.body_content_type = "html" if message.body_is_html else "text"
        record.envelope = {
            "graph_message_id": message.message_id,
            "folder": self._config.mail_folder,
            "was_unread": not message.is_read,
        }
        return record

    # -- Attachments ---------------------------------------------------------

    async def _download_attachments(
        self,
        message: GraphMessage,
        record: MailIntakeMessage,
        summary: MailIntakeSummary,
    ) -> list[_Downloaded]:
        """Fetch what may be stored, and write a row for everything that may not.

        The size gate is applied to Graph's declared size, before the download:
        refusing a 60MB file after transferring it is not defensive, it is just
        late.
        """
        if not message.has_attachments:
            return []

        metadata = await self._client.list_attachments(message.message_id)
        record.attachment_count = len(metadata)
        limit = self._fnol_config.max_document_bytes
        remaining = self._fnol_config.max_documents_per_case

        downloads: list[_Downloaded] = []
        for item in metadata:
            reason = self._refusal(item, remaining=remaining, limit=limit)
            if reason is not None:
                await self._record_attachment(
                    record, item, status=MailAttachmentStatus.SKIPPED, detail=reason
                )
                summary.attachments_skipped += 1
                logger.info(
                    "mail_intake_attachment_skipped",
                    graph_message_id=message.message_id,
                    filename=item.name,
                    size_bytes=item.size_bytes,
                    reason=reason,
                )
                continue

            try:
                content = await self._client.download_attachment(
                    message.message_id, item.attachment_id
                )
            except GraphError as exc:
                # One attachment that will not come down is not a reason to lose
                # the notification that carried it.
                await self._record_attachment(
                    record, item, status=MailAttachmentStatus.FAILED, detail=exc.message
                )
                summary.attachments_failed += 1
                logger.warning(
                    "mail_intake_attachment_download_failed",
                    graph_message_id=message.message_id,
                    filename=item.name,
                    error=exc.message,
                )
                continue

            if not content:
                await self._record_attachment(
                    record,
                    item,
                    status=MailAttachmentStatus.SKIPPED,
                    detail="Microsoft Graph returned no content for this attachment.",
                )
                summary.attachments_skipped += 1
                continue

            downloads.append(_Downloaded(metadata=item, content=content))
            remaining -= 1

        return downloads

    def _refusal(self, item: GraphAttachmentMetadata, *, remaining: int, limit: int) -> str | None:
        """Why this attachment will not be stored, or `None` to store it."""
        if not item.is_file:
            return (
                f"{item.name} is a {item.odata_type.rsplit('.', 1)[-1]} rather than a file, "
                "which carries no document to store."
            )
        if item.is_inline and not self._config.include_inline_attachments:
            return f"{item.name} is embedded in the message body — usually a signature image."
        if item.size_bytes <= 0:
            return f"{item.name} is empty."
        if item.size_bytes > limit:
            return (
                f"{item.name} is {item.size_bytes / 1_000_000:.0f} MB, over the "
                f"{limit / 1_000_000:.0f} MB limit for claim attachments."
            )
        if remaining <= 0:
            return (
                f"{item.name} was not stored: the notification already carries the maximum "
                f"of {self._fnol_config.max_documents_per_case} documents."
            )
        return None

    async def _attach(
        self,
        *,
        case: FNOLCase,
        download: _Downloaded,
        record: MailIntakeMessage,
        summary: MailIntakeSummary,
    ) -> None:
        """Store one downloaded attachment against the notice.

        The document service is what validates it — the same validation a
        browser upload gets — so a refusal here is recorded as a skip rather
        than raised: the broker's covering email is still a notification.
        """
        item = download.metadata
        try:
            document = await self._fnol.attach_document(
                case,
                filename=item.name,
                content=download.content,
                content_type=item.content_type,
                source="email_attachment",
                actor=INTAKE_ACTOR,
            )
        except ValidationError as exc:
            await self._record_attachment(
                record, item, status=MailAttachmentStatus.SKIPPED, detail=exc.message
            )
            summary.attachments_skipped += 1
            logger.info(
                "mail_intake_attachment_refused",
                reference=case.reference,
                filename=item.name,
                reason=exc.message,
            )
            return

        await self._record_attachment(
            record,
            item,
            status=MailAttachmentStatus.STORED,
            checksum=document.checksum_sha256,
            storage_key=document.storage_key,
            fnol_document_id=document.id,
        )
        summary.attachments_stored += 1

    async def _record_attachment(
        self,
        record: MailIntakeMessage,
        item: GraphAttachmentMetadata,
        *,
        status: MailAttachmentStatus,
        detail: str | None = None,
        checksum: str | None = None,
        storage_key: str | None = None,
        fnol_document_id: uuid.UUID | None = None,
    ) -> MailIntakeAttachment:
        """Write — or update — the ledger row for one attachment.

        Updated in place on a retry, so a message collected on its third attempt
        carries three outcomes for one attachment as one row, not three.

        The lookup is a query rather than a walk over `record.attachments`: the
        record has just been flushed, so touching its collection would be a lazy
        load — synchronous IO inside async code, which asyncpg refuses outright.
        """
        existing = await self._messages.find_attachment(record.id, item.attachment_id)
        attachment = existing or MailIntakeAttachment(
            mail_intake_message_id=record.id,
            graph_attachment_id=item.attachment_id,
        )
        attachment.filename = item.name[:255]
        attachment.content_type = item.content_type
        attachment.size_bytes = max(item.size_bytes, 0)
        attachment.is_inline = item.is_inline
        attachment.status = status.value
        attachment.detail = detail
        attachment.checksum_sha256 = checksum
        attachment.storage_key = storage_key
        attachment.fnol_document_id = fnol_document_id

        if existing is None:
            self._messages.add_attachment(attachment)
        return attachment

    # -- Mailbox state -------------------------------------------------------

    async def _settle_mailbox(self, record: MailIntakeMessage, message: GraphMessage) -> None:
        """Mark, and optionally move, the collected message.

        Best-effort by design. The notice is already stored; failing the message
        now because a flag would not set would mean re-collecting it, and the
        only thing worse than an unread processed message is a duplicate claim.
        """
        if self._config.mark_as_read and not record.marked_read:
            try:
                record.marked_read = await self._client.mark_as_read(message.message_id)
            except GraphError as exc:
                logger.warning(
                    "mail_intake_mark_read_failed",
                    graph_message_id=message.message_id,
                    error=exc.message,
                )

        destination = self._config.move_to_folder
        if destination and not record.moved_to_folder:
            try:
                moved_id = await self._client.move_message(message.message_id, destination)
            except GraphError as exc:
                logger.warning(
                    "mail_intake_move_failed",
                    graph_message_id=message.message_id,
                    destination=destination,
                    error=exc.message,
                )
                return
            if moved_id is not None:
                record.moved_to_folder = destination

    # -- Failure -------------------------------------------------------------

    async def _record_failure(self, message: GraphMessage, exc: Exception) -> None:
        """Write down what went wrong, in a transaction of its own.

        The caller's transaction is rolled back first: whatever failed may have
        left the session unusable, and the failure record is the one write that
        must succeed.
        """
        detail = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARACTERS]
        logger.error(
            "mail_intake_message_failed",
            mailbox=self._client.mailbox,
            graph_message_id=message.message_id,
            error=detail,
            exc_info=exc,
        )

        try:
            await self._session.rollback()
            record = await self._messages.find_existing(
                graph_message_id=message.message_id,
                internet_message_id=message.internet_message_id,
            )
            attempts = (record.attempts if record is not None else 0) + 1
            record = self._upsert_record(record, message)
            record.status = MailIntakeStatus.FAILED
            record.attempts = attempts
            record.last_error = detail
            await self._session.commit()
        except Exception as write_failure:
            await self._session.rollback()
            logger.error(
                "mail_intake_failure_not_recorded",
                graph_message_id=message.message_id,
                error=str(write_failure),
            )

    def _as_email(self, message: GraphMessage, downloads: list[_Downloaded]) -> IncomingEmail:
        """The envelope, in the shape every email channel is normalised from."""
        primary = next(
            (item.address for item in message.to_recipients if item.address),
            self._client.mailbox,
        )
        return IncomingEmail(
            sender=message.sender.as_text() or self._client.mailbox,
            recipient=primary,
            subject=message.subject,
            body=message.body,
            # Clipped to the column the notice stores it in. The full pair is on
            # the ledger row either way.
            message_id=message.dedupe_key[:512],
            received_at=message.received_at,
            thread_id=message.conversation_id,
            cc=[item.as_text() for item in message.cc_recipients],
            headers={
                "graph-message-id": message.message_id,
                "internet-message-id": message.internet_message_id or "",
                "mailbox": self._client.mailbox,
                "to": ", ".join(item.as_text() for item in message.to_recipients),
            },
            attachments=[
                IncomingAttachment(
                    filename=download.metadata.name,
                    content=download.content,
                    content_type=download.metadata.content_type,
                )
                for download in downloads
            ],
            from_broker=self._config.from_broker,
            body_is_html=message.body_is_html,
        )
