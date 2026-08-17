"""Deleting a notification, against a real database.

Marked `integration` because the thing under test is almost entirely the
database's own behaviour. A deletion that reached only the tables SQLAlchemy has
a mapped relationship for would pass every unit test and still leave passages,
dataset values and extraction runs behind — those three have no relationship to
the case and go by `ON DELETE CASCADE`, which only a real Postgres can prove.

Object storage is a filesystem store in a temporary directory and the vector
index is the in-memory fake, so both can be inspected directly: the point is that
the sweep reaches them at all, not that MinIO and Qdrant work.

What is asserted, in order of what would hurt most if it broke:

* a converted notice is refused, and refused *before* anything is removed;
* every table that held a row for the case holds none afterwards;
* the attachment bytes are gone from the store, and the passages from the index;
* the audit trail survives, and gained an event naming who deleted it.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.api.deps.services import build_intelligence, build_pipeline
from app.core.config import settings
from app.core.errors import ConflictError, ExternalServiceError
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import AuditEventType, FNOLChannel, MailIntakeStatus
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedValue, ExtractionRun
from app.models.fnol import (
    FNOLAIAnalysis,
    FNOLCase,
    FNOLDocument,
    FNOLDocumentChunk,
    FNOLException,
    FNOLExtractedField,
    FNOLNote,
    FNOLParty,
    FNOLPolicyMatch,
)
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage
from app.repositories.audit import AuditRepository
from app.repositories.chunks import DocumentChunkRepository
from app.repositories.claim import ClaimRepository
from app.repositories.extraction import ExtractionRunRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.mail_intake import MailIntakeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.reference import ReferenceRepository
from app.services.documents.service import DocumentProcessingService
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.fnol.audit import AuditService
from app.services.fnol.deletion import FNOLDeletionService
from app.services.fnol.ingestion import FNOLIngestionService, IncomingNotification
from app.services.fnol.service import FNOLService
from tests.fakes import FakeEmbeddingProvider, FakeVectorStore

pytestmark = pytest.mark.integration

MESSAGE_ID = "<deletion-flow-1@testbrokers.test>"
ACTOR = "Deletion Test"

#: Enough structure that chunking produces several passages and the deterministic
#: reader finds fields to write — a case with one empty document would delete
#: cleanly for the uninteresting reason that there was nothing to remove.
REPORT_PAGES = [
    "LOSS REPORT\nUnit 12 Meadowbank Park\nPrepared for Meadowbank Cold Storage Limited",
    "Policy number: DELETE-2026-0001\n"
    "Date of loss: 04 May 2026\n"
    "Cause of loss: escape of water from a failed chiller feed\n"
    "Loss location: Unit 12 Meadowbank Park, Doncaster",
    "Estimated loss: GBP 84,000\nRepair estimate: GBP 61,200\nInjuries: none reported.",
]


def build_pdf(pages: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    for body in pages:
        offset = 800
        for line in body.splitlines():
            document.drawString(60, offset, line)
            offset -= 20
        document.showPage()
    document.save()
    return buffer.getvalue()


REPORT_PDF = build_pdf(REPORT_PAGES)


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def store(tmp_path: Path) -> AsyncIterator[FilesystemDocumentStore]:
    """A real store on disk, so "the bytes are gone" is a fact and not a mock call."""
    document_store = FilesystemDocumentStore(tmp_path)
    set_document_store(document_store)
    yield document_store
    set_document_store(None)


@pytest.fixture
async def session() -> AsyncIterator[object]:
    factory = get_session_factory()
    async with factory() as db:
        yield db
        await _cleanup(db)
        await db.commit()


async def _cleanup(db: object) -> None:
    """Remove anything a failed test left behind. The passing path leaves nothing."""
    case_ids = list(
        (await db.execute(select(FNOLCase.id).where(FNOLCase.message_id == MESSAGE_ID)))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    await db.execute(  # type: ignore[attr-defined]
        delete(MailIntakeMessage).where(MailIntakeMessage.graph_message_id.like("deletion-flow-%"))
    )
    if case_ids:
        references = list(
            (await db.execute(select(FNOLCase.reference).where(FNOLCase.id.in_(case_ids))))  # type: ignore[attr-defined]
            .scalars()
            .all()
        )
        await db.execute(  # type: ignore[attr-defined]
            delete(AuditEvent).where(AuditEvent.entity_reference.in_(references))
        )
        await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))  # type: ignore[attr-defined]
    else:
        await db.execute(  # type: ignore[attr-defined]
            delete(AuditEvent).where(AuditEvent.actor == ACTOR)
        )


def deletion_service(db: object, *, vectors: object = None) -> FNOLDeletionService:
    return FNOLDeletionService(
        FNOLRepository(db),  # type: ignore[arg-type]
        ClaimRepository(db),  # type: ignore[arg-type]
        DocumentChunkRepository(db),  # type: ignore[arg-type]
        ExtractionRunRepository(db),  # type: ignore[arg-type]
        MailIntakeRepository(db),  # type: ignore[arg-type]
        NotificationRepository(db),  # type: ignore[arg-type]
        AuditService(AuditRepository(db)),  # type: ignore[arg-type]
        documents=DocumentProcessingService(),
        vectors=vectors,  # type: ignore[arg-type]
    )


async def make_case(db: object, *, vectors: FakeVectorStore) -> FNOLCase:
    """A notice as a working desk holds one: read, indexed, annotated, and collected.

    Built through the real services rather than by inserting rows, so the test is
    deleting what the application actually creates — including the notification
    body written out as a document, which a hand-built fixture would omit and
    which is one of the objects the sweep has to remove.
    """
    cases = FNOLRepository(db)  # type: ignore[arg-type]
    audit = AuditService(AuditRepository(db))  # type: ignore[arg-type]
    ingestion = FNOLIngestionService(cases, ReferenceRepository(db), audit)  # type: ignore[arg-type]
    fnol = FNOLService(cases, audit, documents=DocumentProcessingService())

    case, _ = await ingestion.ingest(
        IncomingNotification(
            channel=FNOLChannel.BROKER_EMAIL,
            received_at=datetime.now(UTC),
            body=(
                "Please see the attached loss report for Meadowbank Cold Storage.\n"
                "Policy number: DELETE-2026-0001. We are instructed to notify a claim."
            ),
            message_id=MESSAGE_ID,
            source_metadata={},
        ),
        actor=ACTOR,
    )
    await fnol.attach_document(
        case,
        filename="loss-report.pdf",
        content=REPORT_PDF,
        content_type="application/pdf",
        source="email_attachment",
        actor=ACTOR,
    )

    # The pipeline writes the body document, the fields, the parties, the
    # analyses and the exceptions; indexing writes the passages and the vectors.
    pipeline = build_pipeline(
        db,  # type: ignore[arg-type]
        provider=None,
        embeddings=FakeEmbeddingProvider(),
        vectors=vectors,
    )
    await pipeline.run(case)

    _, _, index = build_intelligence(
        db,  # type: ignore[arg-type]
        embeddings=FakeEmbeddingProvider(),
        vectors=vectors,
    )
    await index.index_case(list(await cases.list_documents(case.id)))

    await fnol.add_note(case, body="Chased the broker for the schedule.", actor=ACTOR)

    # The mailbox ledger row that collected it, with its attachment provenance
    # pointing at the document the bytes actually live under.
    document = next(
        row for row in await cases.list_documents(case.id) if row.filename == "loss-report.pdf"
    )
    message = MailIntakeMessage(
        mailbox="claims@carrier.example",
        graph_message_id=f"deletion-flow-{uuid.uuid4()}",
        internet_message_id=MESSAGE_ID,
        subject="FNOL - Meadowbank Cold Storage",
        sender_address="broker@testbrokers.test",
        received_at=datetime.now(UTC),
        has_attachments=True,
        attachment_count=1,
        status=MailIntakeStatus.PROCESSED.value,
        fnol_case_id=case.id,
    )
    message.attachments.append(
        MailIntakeAttachment(
            graph_attachment_id="att-1",
            filename="loss-report.pdf",
            content_type="application/pdf",
            size_bytes=len(REPORT_PDF),
            storage_key=document.storage_key,
            fnol_document_id=document.id,
        )
    )
    db.add(message)  # type: ignore[attr-defined]

    await db.commit()  # type: ignore[attr-defined]
    return case


async def count(db: object, model: object, case_id: uuid.UUID) -> int:
    statement = select(func.count(model.id)).where(model.fnol_case_id == case_id)  # type: ignore[attr-defined]
    return int((await db.execute(statement)).scalar_one())  # type: ignore[attr-defined]


async def storage_keys(db: object, case_id: uuid.UUID) -> list[str]:
    """Read from the table rather than from `case.documents`.

    The pipeline writes the notification body as a document of its own through
    the repository, so the case's loaded collection is a snapshot from before it
    existed — and that document is one of the objects the sweep has to remove.
    """
    return [document.storage_key for document in await FNOLRepository(db).list_documents(case_id)]  # type: ignore[arg-type]


class TestCasePurge:
    async def test_every_store_that_held_the_notice_lets_go_of_it(
        self, session: object, store: FilesystemDocumentStore
    ) -> None:
        vectors = FakeVectorStore()
        case = await make_case(session, vectors=vectors)
        case_id, reference = case.id, case.reference

        # The state being deleted from, asserted rather than assumed: a test that
        # deleted an empty case would pass without proving anything.
        keys = await storage_keys(session, case_id)
        assert len(keys) == 2, "the attachment and the notification body"
        assert await count(session, FNOLDocumentChunk, case_id) > 0
        assert await count(session, FNOLExtractedField, case_id) > 0
        assert vectors.points, "the passages were vectorised"
        for key in keys:
            assert await store.get(key), key

        service = deletion_service(session, vectors=vectors)
        receipt = await service.remove(case, actor=ACTOR, reason="Test submission.")
        await session.commit()  # type: ignore[attr-defined]
        await service.purge_stores(receipt)

        # --- Postgres --------------------------------------------------------
        assert await session.get(FNOLCase, case_id) is None  # type: ignore[attr-defined]
        for model in (
            FNOLDocument,
            FNOLDocumentChunk,
            FNOLExtractedField,
            FNOLParty,
            FNOLPolicyMatch,
            FNOLException,
            FNOLAIAnalysis,
            FNOLNote,
            ExtractionRun,
            ExtractedValue,
        ):
            assert await count(session, model, case_id) == 0, model.__name__

        remaining = int(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count(MailIntakeMessage.id)).where(
                        MailIntakeMessage.internet_message_id == MESSAGE_ID
                    )
                )
            ).scalar_one()
        )
        assert remaining == 0, "the mailbox ledger row went with the notice"
        attachments = int(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count(MailIntakeAttachment.id)).where(
                        MailIntakeAttachment.filename == "loss-report.pdf"
                    )
                )
            ).scalar_one()
        )
        assert attachments == 0

        # --- Object storage and the index ------------------------------------
        for key in keys:
            with pytest.raises(ExternalServiceError):
                await store.get(key)
        assert vectors.points == {}

        # --- The receipt -----------------------------------------------------
        assert receipt.reference == reference
        assert receipt.blobs_removed == 2
        assert receipt.blobs_failed == ()
        assert receipt.vectors_cleared is True
        assert receipt.warnings == []
        assert receipt.records["documents"] == 2
        assert receipt.records["chunks"] > 0
        assert receipt.records["mail_messages"] == 1
        assert receipt.records["mail_attachments"] == 1
        assert receipt.total_records == sum(receipt.records.values())

    async def test_the_audit_trail_outlives_the_notice(self, session: object) -> None:
        vectors = FakeVectorStore()
        case = await make_case(session, vectors=vectors)
        case_id, reference = case.id, case.reference

        service = deletion_service(session, vectors=vectors)
        receipt = await service.remove(case, actor=ACTOR, reason="Duplicate submission.")
        await session.commit()  # type: ignore[attr-defined]
        await service.purge_stores(receipt)

        events = list(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(AuditEvent)
                    .where(AuditEvent.entity_id == case_id)
                    .order_by(AuditEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) > 1, "the history before the deletion is kept"

        deleted = [
            event for event in events if event.event_type == AuditEventType.FNOL_DELETED.value
        ]
        assert len(deleted) == 1
        assert deleted[0].actor == ACTOR
        assert deleted[0].entity_reference == reference
        assert "Duplicate submission." in deleted[0].summary
        assert deleted[0].context["records"]["documents"] == 2

    async def test_a_case_that_became_a_claim_is_refused_untouched(
        self, session: object, store: FilesystemDocumentStore
    ) -> None:
        vectors = FakeVectorStore()
        case = await make_case(session, vectors=vectors)
        case_id = case.id
        keys = await storage_keys(session, case_id)

        # Standing in for claim creation, which needs a policy, a handler and a
        # clean notice. The refusal is on the link, and the link is this column.
        from app.models.claim import Claim

        claim = Claim(
            reference=f"CLM-TEST-{uuid.uuid4().hex[:8].upper()}",
            fnol_case_id=case_id,
            status="open",
            line_of_business="property",
            currency="GBP",
            reported_at=datetime.now(UTC),
        )
        session.add(claim)  # type: ignore[attr-defined]
        await session.flush()  # type: ignore[attr-defined]

        service = deletion_service(session, vectors=vectors)
        with pytest.raises(ConflictError, match="cannot be deleted"):
            await service.remove(case, actor=ACTOR)

        # Refused *before* anything was removed — the assertion that matters, since
        # a guard that runs after the first store is swept is not a guard.
        assert await session.get(FNOLCase, case_id) is not None  # type: ignore[attr-defined]
        assert await count(session, FNOLDocument, case_id) == 2
        assert vectors.points != {}
        for key in keys:
            assert await store.get(key), key

        await session.execute(delete(Claim).where(Claim.id == claim.id))  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

    async def test_a_store_that_refuses_does_not_fail_the_deletion(self, session: object) -> None:
        """The rows are already committed; a broken bucket can only be reported.

        The alternative — a 502 for a case that has in fact gone — leaves the
        officer reloading a notice that no longer exists.
        """
        vectors = FakeVectorStore()
        case = await make_case(session, vectors=vectors)

        class BrokenStore(FilesystemDocumentStore):
            async def delete(self, key: str) -> None:
                raise ConnectionError("The bucket is unreachable.")

        set_document_store(BrokenStore(Path("/tmp/claims-deletion-broken")))
        service = deletion_service(session, vectors=vectors)
        receipt = await service.remove(case, actor=ACTOR)
        await session.commit()  # type: ignore[attr-defined]
        await service.purge_stores(receipt)

        assert receipt.blobs_removed == 0
        assert len(receipt.blobs_failed) == 2
        assert receipt.warnings and "object storage" in receipt.warnings[0]
        # The index is swept regardless: one store being down must not stop the other.
        assert receipt.vectors_cleared is True
        assert vectors.points == {}
