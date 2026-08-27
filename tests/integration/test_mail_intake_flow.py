"""Mailbox intake against a real database.

Marked `integration` because it needs Postgres. The unit tests fake the session
and the repositories; this one proves the things a fake cannot: that the ledger
rows insert, that the unique constraints are what make a second poll a no-op,
and that an attachment arrives as an `FNOLDocument` with its bytes in the store.

Microsoft Graph is still stubbed — the point is the persistence, and a test that
needed a tenant would not run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import (
    FNOLChannel,
    MailAttachmentStatus,
    MailIntakeStatus,
    ProcessingState,
)
from app.integrations.graph.messages import (
    FILE_ATTACHMENT_TYPE,
    GraphAttachmentMetadata,
    GraphMessage,
    GraphRecipient,
)
from app.models.audit import AuditEvent
from app.models.fnol import FNOLCase, FNOLDocument
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.mail.runner import build_mail_intake_service

pytestmark = pytest.mark.integration

MAILBOX = "integration-claims@carrier.test"
GRAPH_ID = "AAMk-integration-1"
INTERNET_ID = "<integration-1@testbrokers.test>"
ESTIMATE = b"Item,Cost\nMake safe,12000.00\nReinstate finishing shop,180000.00\n"


class StubMailClient:
    """The mailbox, without Microsoft in it."""

    def __init__(self, message: GraphMessage, attachments: list[GraphAttachmentMetadata]) -> None:
        self.mailbox = MAILBOX
        self._message = message
        self._attachments = attachments
        self.marked_read: list[str] = []

    async def list_messages(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> list[GraphMessage]:
        if since is not None and self._message.received_at < since:
            return []
        return [self._message]

    async def folder_stats(self, folder: str | None = None) -> tuple[int, int]:
        return 1, 0

    async def list_attachments(self, message_id: str) -> list[GraphAttachmentMetadata]:
        return self._attachments

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        return ESTIMATE

    async def mark_as_read(self, message_id: str) -> bool:
        self.marked_read.append(message_id)
        return True

    async def move_message(self, message_id: str, destination: str) -> str | None:
        return None


def broker_message() -> GraphMessage:
    return GraphMessage(
        message_id=GRAPH_ID,
        internet_message_id=INTERNET_ID,
        conversation_id="conv-integration-1",
        subject="FNOL - Testfield Manufacturing - fire - TST-2026-0001",
        body=(
            "Policy number: TST-2026-0001\n"
            "Insured: Testfield Manufacturing Limited\n"
            "Cause: fire\n"
            "Estimated loss: GBP 320,000\n"
        ),
        body_is_html=False,
        received_at=datetime.now(UTC),
        has_attachments=True,
        is_read=False,
        sender=GraphRecipient(name="Alice Wren", address="claims@testbrokers.test"),
        to_recipients=[GraphRecipient(name="Claims", address=MAILBOX)],
    )


def estimate_attachment() -> GraphAttachmentMetadata:
    return GraphAttachmentMetadata(
        attachment_id="att-integration-1",
        name="estimate.csv",
        content_type="text/csv",
        size_bytes=len(ESTIMATE),
        is_inline=False,
        odata_type=FILE_ATTACHMENT_TYPE,
    )


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def _documents(tmp_path: Path) -> AsyncIterator[None]:
    """Attachment bytes go to a temporary directory, not to MinIO."""
    set_document_store(FilesystemDocumentStore(tmp_path))
    yield
    set_document_store(None)


@pytest.fixture
async def session() -> AsyncIterator[object]:
    factory = get_session_factory()
    async with factory() as db:
        yield db
        await _cleanup(db)
        await db.commit()


async def _cleanup(db: object) -> None:
    """Remove this test's rows, children before parents."""
    case_ids = list(
        (
            await db.execute(
                select(MailIntakeMessage.fnol_case_id).where(MailIntakeMessage.mailbox == MAILBOX)
            )
        )
        .scalars()
        .all()
    )
    case_ids = [case_id for case_id in case_ids if case_id is not None]

    await db.execute(delete(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX))
    if case_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(case_ids)))
        await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))


class TestMailboxIntakeAgainstPostgres:
    async def test_a_message_lands_as_a_queued_notice_with_its_document(
        self, session: object
    ) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]

        summary = await service.poll()

        assert (summary.ingested, summary.attachments_stored, summary.failed) == (1, 1, 0)
        assert client.marked_read == [GRAPH_ID]

        record = (
            (
                await session.execute(
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.internet_message_id == INTERNET_ID
        assert record.marked_read is True
        assert record.attempts == 1
        assert record.fnol_case_id is not None

        case = await session.get(FNOLCase, record.fnol_case_id)
        assert case is not None
        assert case.channel == FNOLChannel.BROKER_EMAIL
        # Queued, not processed: extraction is the next phase.
        assert case.processing_state == ProcessingState.QUEUED
        assert case.message_id == INTERNET_ID

        document = (
            (
                await session.execute(
                    select(FNOLDocument).where(FNOLDocument.fnol_case_id == case.id)
                )
            )
            .scalars()
            .one()
        )
        assert document.filename == "estimate.csv"
        assert document.source == "email_attachment"
        assert document.size_bytes == len(ESTIMATE)

        attachment = (
            (
                await session.execute(
                    select(MailIntakeAttachment).where(
                        MailIntakeAttachment.mail_intake_message_id == record.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert attachment.status == MailAttachmentStatus.STORED
        # The ledger points at the document the extraction phase will read.
        assert attachment.fnol_document_id == document.id
        assert attachment.checksum_sha256 == document.checksum_sha256

    async def test_a_second_poll_of_the_same_message_creates_nothing(self, session: object) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]

        first = await service.poll()
        second = await service.poll()

        assert (first.ingested, second.ingested) == (1, 0)
        assert second.duplicates == 1

        cases = (
            (await session.execute(select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)))
            .scalars()
            .all()
        )
        assert len(cases) == 1

        records = (
            (
                await session.execute(
                    select(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX)
                )
            )
            .scalars()
            .all()
        )
        assert len(records) == 1
        assert records[0].attempts == 1  # the second poll did no work

    async def test_a_redelivery_under_a_new_graph_id_is_recognised(self, session: object) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll()

        # Outlook mints a new id; the sending server's Message-ID does not change.
        redelivered = broker_message()
        redelivered.message_id = "AAMk-integration-2"
        resent = StubMailClient(redelivered, [estimate_attachment()])
        service = build_mail_intake_service(session, client=resent)  # type: ignore[arg-type]

        summary = await service.poll()

        assert (summary.ingested, summary.duplicates) == (0, 1)
        total = (
            await session.execute(
                select(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX)
            )
        ).scalars()
        assert len(total.all()) == 1
