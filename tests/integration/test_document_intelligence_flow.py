"""The whole path, against a real database.

An email arrives with a PDF attached; the attachment is stored and read; its text is
cut into passages with real rows and a real generated `tsvector`; the pipeline extracts
fields citing those passages; and the citation resolves back to a page and a rectangle.

Marked `integration` because it needs Postgres, and because the parts a fake cannot
prove are exactly the parts that matter here:

* the `content_tsv` generated column and its GIN index really rank a keyword search;
* `ON DELETE CASCADE` really removes a document's passages;
* `source_chunk_id`'s `ON DELETE SET NULL` really preserves the *value* when a
  re-index replaces the passage it cited;
* re-running the whole flow really writes nothing and embeds nothing.

The embedding provider and the vector store are still fakes: the point is the
persistence and the arithmetic, and a test that needed an API key would not run.
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
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import (
    DocumentExtractionStatus,
    DocumentIndexStatus,
    FNOLChannel,
    ProcessingState,
)
from app.models.audit import AuditEvent
from app.models.fnol import (
    FNOLAIAnalysis,
    FNOLCase,
    FNOLDocument,
    FNOLDocumentChunk,
    FNOLExtractedField,
)
from app.repositories.audit import AuditRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.reference import ReferenceRepository
from app.services.documents.service import DocumentProcessingService
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.fnol.audit import AuditService
from app.services.fnol.ingestion import FNOLIngestionService
from app.services.fnol.service import FNOLService
from app.services.intelligence.highlight import (
    locate_in_chunk,
    page_for_offset,
    resolve_pdf_rects,
)
from app.services.intelligence.runner import run_case_index
from tests.fakes import FakeEmbeddingProvider, FakeVectorStore

pytestmark = pytest.mark.integration

MESSAGE_ID = "<docint-flow-1@testbrokers.test>"

#: Long enough that chunking produces several passages, and structured the way a
#: survey report is: a cover page, then the facts, then the numbers.
SURVEY_PAGES = [
    "SURVEY REPORT\nUnit 7 Harborview Estate\nPrepared for Harborview Logistics Limited\n"
    "Instructed by Odele Loss Adjusting on 09 March 2026",
    "Policy number: DOCINT-2026-0001\n"
    "Date of loss: 08 March 2026\n"
    "Cause of loss: escape of water from a failed riser joint\n"
    "Loss location: Unit 7 Harborview Estate, Kingston upon Hull",
    "Estimated loss: GBP 128,000\n"
    "Repair estimate: GBP 96,400\n"
    "Business interruption is expected for eleven weeks\n"
    "Police reference: none. Injuries: none reported.",
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


SURVEY_PDF = build_pdf(SURVEY_PAGES)


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def _documents(tmp_path: Path) -> AsyncIterator[None]:
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
        (await db.execute(select(FNOLCase.id).where(FNOLCase.message_id == MESSAGE_ID)))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    if not case_ids:
        return

    references = list(
        (await db.execute(select(FNOLCase.reference).where(FNOLCase.id.in_(case_ids))))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    await db.execute(  # type: ignore[attr-defined]
        delete(FNOLDocumentChunk).where(FNOLDocumentChunk.fnol_case_id.in_(case_ids))
    )
    await db.execute(  # type: ignore[attr-defined]
        delete(AuditEvent).where(AuditEvent.entity_reference.in_(references))
    )
    await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))  # type: ignore[attr-defined]


async def make_case(db: object) -> FNOLCase:
    """A notice with the survey report attached, the way email intake leaves it."""
    cases = FNOLRepository(db)  # type: ignore[arg-type]
    audit = AuditService(AuditRepository(db))  # type: ignore[arg-type]
    ingestion = FNOLIngestionService(cases, ReferenceRepository(db), audit)  # type: ignore[arg-type]
    fnol = FNOLService(cases, audit, documents=DocumentProcessingService())

    from app.services.fnol.ingestion import IncomingNotification

    case, _ = await ingestion.ingest(
        IncomingNotification(
            channel=FNOLChannel.BROKER_EMAIL,
            received_at=datetime.now(UTC),
            body=(
                "Please see the attached survey report for Harborview Logistics.\n"
                "We are instructed to notify a claim."
            ),
            message_id=MESSAGE_ID,
            source_metadata={},
        ),
        actor="integration",
    )
    await fnol.attach_document(
        case,
        filename="survey-report.pdf",
        content=SURVEY_PDF,
        content_type="application/pdf",
        source="email_attachment",
        actor="integration",
    )
    await cases.flush()
    return case


def make_pipeline(db: object, *, embeddings: object = None, vectors: object = None) -> object:
    return build_pipeline(
        db,  # type: ignore[arg-type]
        # No LLM: the deterministic reader is what runs, which is the right choice for
        # a persistence test. The citation path is exercised by the retrieval tests,
        # and by `test_a_cited_field_resolves_to_a_page_and_a_rectangle` below.
        provider=None,
        embeddings=embeddings,  # type: ignore[arg-type]
        vectors=vectors,  # type: ignore[arg-type]
    )


class TestIndexingPersistence:
    async def test_a_pdf_attachment_becomes_passages_with_pages(self, session: object) -> None:
        case = await make_case(session)
        _, _, index = build_intelligence(
            session,  # type: ignore[arg-type]
            embeddings=FakeEmbeddingProvider(),
            vectors=FakeVectorStore(),
        )
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        documents = list(await cases.list_documents(case.id))

        outcome = await index.index_case(documents)
        await session.commit()  # type: ignore[attr-defined]

        assert outcome.indexed == 1
        document = documents[0]
        assert document.extraction_status == DocumentExtractionStatus.EXTRACTED
        assert document.text_extractor == "pdf_text_layer"
        assert document.index_status == DocumentIndexStatus.INDEXED
        assert document.chunk_count > 0
        assert document.embedded_chunk_count == document.chunk_count

        rows = list(
            await session.execute(  # type: ignore[attr-defined]
                select(FNOLDocumentChunk)
                .where(FNOLDocumentChunk.fnol_document_id == document.id)
                .order_by(FNOLDocumentChunk.chunk_index)
            )
        )
        chunks = [row[0] for row in rows]
        assert len(chunks) == document.chunk_count

        # The invariant everything visible rests on, verified against stored rows.
        for chunk in chunks:
            assert chunk.content == document.extracted_text[chunk.char_start : chunk.char_end]
            assert chunk.page_number is not None
            assert chunk.vector_point_id is not None

    async def test_the_generated_tsvector_ranks_a_keyword_search(self, session: object) -> None:
        # Provable only against a real Postgres: the column is `GENERATED ALWAYS`, so
        # nothing in Python ever writes it.
        case = await make_case(session)
        chunks_repo, _, index = build_intelligence(session)  # type: ignore[arg-type]
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        await index.index_case(list(await cases.list_documents(case.id)))
        await session.commit()  # type: ignore[attr-defined]

        found = await chunks_repo.keyword_search(case.id, "escape of water riser", limit=5)
        assert found
        assert "riser" in found[0][0].content
        assert found[0][1] > 0.0

    async def test_re_running_the_flow_writes_nothing_and_embeds_nothing(
        self, session: object
    ) -> None:
        case = await make_case(session)
        embeddings = FakeEmbeddingProvider()
        store = FakeVectorStore()
        cases = FNOLRepository(session)  # type: ignore[arg-type]

        _, _, index = build_intelligence(
            session,
            embeddings=embeddings,
            vectors=store,  # type: ignore[arg-type]
        )
        documents = list(await cases.list_documents(case.id))
        await index.index_case(documents)
        await session.commit()  # type: ignore[attr-defined]

        first_signature = (await index.index_case(documents)).signature
        chunk_ids = set(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLDocumentChunk.id).where(FNOLDocumentChunk.fnol_case_id == case.id)
                )
            )
            .scalars()
            .all()
        )

        # Second run: identical passages, identical signature, no provider call.
        second = await index.index_case(documents)
        await session.commit()  # type: ignore[attr-defined]

        assert embeddings.document_calls == 1
        assert store.upsert_calls == 1
        assert second.signature == first_signature
        again = set(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLDocumentChunk.id).where(FNOLDocumentChunk.fnol_case_id == case.id)
                )
            )
            .scalars()
            .all()
        )
        assert again == chunk_ids

    async def test_deleting_a_document_cascades_its_passages(self, session: object) -> None:
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        _, _, index = build_intelligence(session)  # type: ignore[arg-type]
        documents = list(await cases.list_documents(case.id))
        await index.index_case(documents)
        await session.commit()  # type: ignore[attr-defined]

        before = await _count_chunks(session, case.id)
        assert before > 0

        await session.execute(  # type: ignore[attr-defined]
            delete(FNOLDocument).where(FNOLDocument.id == documents[0].id)
        )
        await session.commit()  # type: ignore[attr-defined]

        assert await _count_chunks(session, case.id) == 0


class TestPipelineIntegration:
    async def test_the_pipeline_indexes_then_extracts(self, session: object) -> None:
        case = await make_case(session)
        pipeline = make_pipeline(
            session, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
        )

        result = await pipeline.run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        # Two: the survey report, and the notification body the pipeline writes
        # out as a document so that a value read from it can be cited too.
        assert result.documents_indexed == 2
        assert result.chunks_indexed > 0
        assert case.processing_state == ProcessingState.COMPLETED

        # The deterministic reader found the policy number in the attachment's text,
        # which is only possible because the attachment was read at all.
        fields = {
            row[0].field_path: row[0]
            for row in await session.execute(  # type: ignore[attr-defined]
                select(FNOLExtractedField).where(FNOLExtractedField.fnol_case_id == case.id)
            )
        }
        assert fields["policy.policy_number"].value_text == "DOCINT-2026-0001"

    async def test_a_second_pipeline_run_reuses_the_extraction(self, session: object) -> None:
        case = await make_case(session)
        pipeline = make_pipeline(
            session, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
        )

        await pipeline.run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]
        second = await pipeline.run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        # The index signature is stable, so the extraction fingerprint is stable, so
        # nothing is recomputed. This is the economy the pipeline docstring claims.
        assert second.extraction_reused is True

    async def test_the_retrieval_trace_is_recorded_with_the_extraction(
        self, session: object
    ) -> None:
        case = await make_case(session)
        pipeline = make_pipeline(
            session, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
        )
        await pipeline.run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        analysis = (
            await session.execute(  # type: ignore[attr-defined]
                select(FNOLAIAnalysis).where(
                    FNOLAIAnalysis.fnol_case_id == case.id,
                    FNOLAIAnalysis.kind == "extraction",
                )
            )
        ).scalar_one()

        # Present even when retrieval declined to narrow the prompt: "whole-corpus" is
        # itself the answer to "why did the model read that".
        assert "retrieval" in analysis.result


class TestCitationAndHighlight:
    async def test_a_cited_field_resolves_to_a_page_and_a_rectangle(self, session: object) -> None:
        """The click-through, end to end.

        Rather than depending on a model choosing to cite, this writes the citation the
        way `apply` writes it and then walks the exact path the evidence endpoint walks.
        What is being proved is the resolution chain — passage to page to rectangle —
        against rows that came out of Postgres.
        """
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        chunks_repo, _, index = build_intelligence(session)  # type: ignore[arg-type]
        documents = list(await cases.list_documents(case.id))
        await index.index_case(documents)
        await session.commit()  # type: ignore[attr-defined]

        document = documents[0]
        passage = next(
            row
            for row in await chunks_repo.list_for_document(document.id)
            if "DOCINT-2026-0001" in row.content
        )

        await cases.upsert_field(
            case.id,
            field_path="policy.policy_number",
            section="policy",
            label="Policy number",
            value_text="DOCINT-2026-0001",
            confidence=0.9,
            source="ai",
            source_document_id=document.id,
            source_chunk_id=passage.id,
            evidence_snippet="Policy number: DOCINT-2026-0001",
        )
        await session.commit()  # type: ignore[attr-defined]

        field = await cases.get_field(case.id, "policy.policy_number")
        assert field is not None and field.source_chunk_id == passage.id

        # --- the evidence endpoint's resolution chain -------------------------
        cited = await chunks_repo.get(field.source_chunk_id)
        assert cited is not None

        start, end, text = locate_in_chunk(cited.content, cited.char_start, field.evidence_snippet)
        assert text == "Policy number: DOCINT-2026-0001"
        # The offsets address the stored document text exactly.
        assert document.extracted_text[start:end] == text

        located = page_for_offset(document.page_offsets, start)
        assert located is not None
        # The policy number is on the second page of the fixture.
        assert located[0] + 1 == 2

        content = await DocumentProcessingService().fetch(document.storage_key)
        rects, note = resolve_pdf_rects(content, page_index=located[0], text=text)

        assert note is None
        assert len(rects) == 1
        assert rects[0].page_number == 2
        assert rects[0].x0 == pytest.approx(60.0, abs=1.0)
        assert rects[0].x1 > rects[0].x0
        assert rects[0].page_width > 0 and rects[0].page_height > 0

    async def test_a_re_index_clears_the_citation_but_keeps_the_value(
        self, session: object
    ) -> None:
        """`ON DELETE SET NULL`, and why it is not `CASCADE`.

        Re-indexing replaces a document's passages. If the citation cascaded, the
        officer would lose the *value* too — and a value with a stale citation is
        recoverable while a deleted value is not.
        """
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        chunks_repo, _, index = build_intelligence(session)  # type: ignore[arg-type]
        documents = list(await cases.list_documents(case.id))
        await index.index_case(documents)
        await session.commit()  # type: ignore[attr-defined]

        passage = (await chunks_repo.list_for_document(documents[0].id))[0]
        await cases.upsert_field(
            case.id,
            field_path="loss.cause_of_loss",
            section="loss",
            label="Cause of loss",
            value_text="escape of water",
            confidence=0.8,
            source="ai",
            source_document_id=documents[0].id,
            source_chunk_id=passage.id,
            evidence_snippet="Cause of loss: escape of water",
        )
        await session.commit()  # type: ignore[attr-defined]

        await index.index_case(documents, force=True)
        await session.commit()  # type: ignore[attr-defined]

        field = await cases.get_field(case.id, "loss.cause_of_loss")
        assert field is not None
        assert field.value_text == "escape of water"
        assert field.source_chunk_id is None


class TestQueueSeam:
    async def test_a_queued_case_is_claimed_once(self, session: object) -> None:
        """The seam `docs/mail-intake.md` promised and nothing previously read.

        Claiming moves the state inside the selecting transaction, so a second claim
        finds nothing — which is what stops two beat ticks from both working a notice.
        """
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        FNOLService(
            cases, AuditService(AuditRepository(session)), documents=DocumentProcessingService()
        ).mark_queued(case)  # type: ignore[arg-type]
        await session.commit()  # type: ignore[attr-defined]

        claimed = await cases.claim_queued(limit=10)
        assert case.id in {row.id for row in claimed}
        assert case.processing_state == ProcessingState.PROCESSING
        await session.commit()  # type: ignore[attr-defined]

        assert case.id not in {row.id for row in await cases.claim_queued(limit=10)}

    async def test_a_case_stuck_processing_is_requeued(self, session: object) -> None:
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        case.processing_state = ProcessingState.PROCESSING
        case.processing_started_at = datetime(2020, 1, 1, tzinfo=UTC)
        await session.commit()  # type: ignore[attr-defined]

        released = await cases.release_stale_processing(older_than_minutes=30)
        await session.commit()  # type: ignore[attr-defined]

        assert released >= 1
        assert case.processing_state == ProcessingState.QUEUED

    async def test_a_document_stuck_indexing_is_returned_to_pending(self, session: object) -> None:
        """A worker killed mid-index leaves `indexing`, which nothing else picks up.

        The staleness clock is `updated_at`, and a database trigger owns that column —
        so this cannot backdate the row to make it look old, because the trigger
        overwrites whatever an UPDATE writes. The threshold is set to zero instead,
        which exercises the same comparison against the real timestamp the claim wrote.
        """
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        document = (await cases.list_documents(case.id))[0]
        document.index_status = DocumentIndexStatus.INDEXING
        await session.commit()  # type: ignore[attr-defined]

        released = await cases.release_stale_indexing(older_than_minutes=0, max_attempts=3)
        await session.commit()  # type: ignore[attr-defined]

        assert released >= 1
        assert document.index_status == DocumentIndexStatus.PENDING
        assert "interrupted" in (document.index_error or "")

    async def test_the_worker_indexes_a_case_in_its_own_session(self, session: object) -> None:
        """The path beat actually takes: a session the runner opens for itself.

        Every other test here hands the index service a session that already has the
        case in its identity map, so a lazy relationship on a document resolves
        without touching the database. The worker never has that — it loads the row
        cold — and a lazy load in an async session raises `MissingGreenlet` rather
        than emitting a query. That is a crash the whole rest of this file could not
        see, and it left the notice stuck in `processing` until the reaper ran.
        """
        case = await make_case(session)
        await session.commit()  # type: ignore[attr-defined]

        summary = await run_case_index(case.id)

        assert summary.case_reference == case.reference
        assert summary.documents == 1
        assert summary.failed == 0
        assert summary.chunks > 0

    async def test_a_document_out_of_attempts_is_failed_rather_than_requeued(
        self, session: object
    ) -> None:
        # The point of an attempt limit is that it is reached.
        case = await make_case(session)
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        document = (await cases.list_documents(case.id))[0]
        document.index_status = DocumentIndexStatus.INDEXING
        document.index_attempts = 3
        await session.commit()  # type: ignore[attr-defined]

        await cases.release_stale_indexing(older_than_minutes=0, max_attempts=3)
        await session.commit()  # type: ignore[attr-defined]

        assert document.index_status == DocumentIndexStatus.FAILED


async def _count_chunks(db: object, case_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(  # type: ignore[attr-defined]
                select(func.count(FNOLDocumentChunk.id)).where(
                    FNOLDocumentChunk.fnol_case_id == case_id
                )
            )
        ).scalar_one()
    )
