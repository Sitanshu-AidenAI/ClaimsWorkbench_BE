"""The index service: idempotency, degradation and failure isolation.

Three properties, each of which is the answer to a question someone will ask in
production:

* *"Why did re-running this cost money?"* — it must not. Running twice on an unchanged
  document does no embedding call and no vector write.
* *"Why is nothing searchable?"* — with no embedding provider the passages still exist
  and are still keyword-searchable. That is `skipped`, not `failed`.
* *"Why did one bad attachment lose the other ten?"* — it must not. Each document's
  outcome is its own.

The repository is a small in-memory fake rather than a real session: this is testing the
service's decision-making, and a database would only make the same assertions slower.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.config import DocumentIntelligenceSettings
from app.domain.enums import DocumentExtractionStatus, DocumentIndexStatus
from app.repositories.chunks import chunk_ref
from app.services.intelligence.indexing import (
    DocumentIndexService,
    TransientIndexError,
    case_signature,
    extraction_signature,
)
from app.services.intelligence.vectors import point_id_for
from tests.fakes import FailingEmbeddingProvider, FakeEmbeddingProvider, FakeVectorStore


@dataclass
class FakeChunkRow:
    """Stands in for `FNOLDocumentChunk`, with the fields the service touches."""

    fnol_document_id: uuid.UUID
    fnol_case_id: uuid.UUID
    chunk_index: int
    chunk_ref: str
    content: str
    content_hash: str
    token_count: int
    char_start: int
    char_end: int
    page_number: int | None = None
    page_from: int | None = None
    page_to: int | None = None
    section_label: str | None = None
    vector_point_id: uuid.UUID | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedded_at: Any = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class FakeDocument:
    """Stands in for `FNOLDocument`."""

    fnol_case_id: uuid.UUID
    extracted_text: str
    checksum_sha256: str = "deadbeef"
    text_extractor: str = "pdf_text_layer"
    text_extractor_version: str = "1"
    extraction_signature: str | None = None
    extraction_status: str = DocumentExtractionStatus.EXTRACTED
    extraction_error: str | None = None
    page_offsets: list[list[int]] | None = None
    index_status: str = DocumentIndexStatus.PENDING
    index_fingerprint: str | None = None
    index_error: str | None = None
    index_attempts: int = 0
    chunk_count: int = 0
    embedded_chunk_count: int = 0
    indexed_at: Any = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class FakeChunkRepository:
    """In-memory passages, keyed by document."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, list[FakeChunkRow]] = {}
        self.replace_calls = 0

    async def flush(self) -> None:
        return None

    async def replace_for_document(self, document: Any, chunks: Any) -> list[FakeChunkRow]:
        self.replace_calls += 1
        rows = [
            FakeChunkRow(
                fnol_document_id=document.id,
                fnol_case_id=document.fnol_case_id,
                chunk_index=chunk.index,
                chunk_ref=chunk_ref(document.id, chunk.index),
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_estimate,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                page_number=chunk.page_number,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                section_label=chunk.section_label,
            )
            for chunk in chunks
        ]
        self.rows[document.id] = rows
        return rows

    async def mark_embedded(
        self, rows: Any, *, point_ids: dict[str, uuid.UUID], model: str, dimension: int
    ) -> int:
        count = 0
        for row in rows:
            point = point_ids.get(row.chunk_ref)
            if point is None:
                continue
            row.vector_point_id = point
            row.embedding_model = model
            row.embedding_dimension = dimension
            count += 1
        return count

    async def clear_vectors_for_document(self, document_id: uuid.UUID) -> None:
        for row in self.rows.get(document_id, []):
            row.vector_point_id = None

    async def list_for_document(self, document_id: uuid.UUID, **_: Any) -> list[FakeChunkRow]:
        return self.rows.get(document_id, [])


LONG_TEXT = "\n".join(
    [
        "Policy number: CP-2026-4471",
        "Date of loss: 08 March 2026",
        "Cause of loss: escape of water from a failed riser joint",
        "Estimated loss: GBP 128,000",
        "Repair estimate: GBP 96,400",
    ]
    * 6
)


@pytest.fixture
def config() -> DocumentIntelligenceSettings:
    return DocumentIntelligenceSettings(
        chunk_tokens=40, chunk_overlap_tokens=4, index_max_attempts=3
    )


def make_service(
    repo: FakeChunkRepository,
    config: DocumentIntelligenceSettings,
    *,
    embeddings: Any = None,
    vectors: Any = None,
) -> DocumentIndexService:
    return DocumentIndexService(repo, embeddings=embeddings, vectors=vectors, config=config)  # type: ignore[arg-type]


class TestIdempotency:
    async def test_a_second_run_embeds_nothing(self, config: DocumentIntelligenceSettings) -> None:
        repo = FakeChunkRepository()
        embeddings = FakeEmbeddingProvider()
        store = FakeVectorStore()
        service = make_service(repo, config, embeddings=embeddings, vectors=store)
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        first = await service.index_document(document)  # type: ignore[arg-type]
        assert first.status == DocumentIndexStatus.INDEXED
        assert first.chunks > 0
        assert embeddings.document_calls == 1

        second = await service.index_document(document)  # type: ignore[arg-type]
        assert second.reused is True
        # The whole point: no provider call, no vector write, no passage rewrite.
        assert embeddings.document_calls == 1
        assert store.upsert_calls == 1
        assert repo.replace_calls == 1

    async def test_force_re_indexes(self, config: DocumentIntelligenceSettings) -> None:
        repo = FakeChunkRepository()
        embeddings = FakeEmbeddingProvider()
        service = make_service(repo, config, embeddings=embeddings, vectors=FakeVectorStore())
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        await service.index_document(document)  # type: ignore[arg-type]
        await service.index_document(document, force=True)  # type: ignore[arg-type]
        assert embeddings.document_calls == 2

    async def test_a_chunk_size_change_re_indexes(self) -> None:
        repo = FakeChunkRepository()
        embeddings = FakeEmbeddingProvider()
        store = FakeVectorStore()
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        small = DocumentIntelligenceSettings(chunk_tokens=40, chunk_overlap_tokens=4)
        await make_service(repo, small, embeddings=embeddings, vectors=store).index_document(
            document  # type: ignore[arg-type]
        )
        first_count = document.chunk_count

        # Same document, different chunk parameters: the passages genuinely differ, so
        # the fingerprint must move and the work must be redone.
        large = DocumentIntelligenceSettings(chunk_tokens=200, chunk_overlap_tokens=4)
        await make_service(repo, large, embeddings=embeddings, vectors=store).index_document(
            document  # type: ignore[arg-type]
        )
        assert embeddings.document_calls == 2
        assert document.chunk_count != first_count

    async def test_point_ids_are_stable_across_runs(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        # The fix for IIF's uuid4 point ids: re-indexing must overwrite, not accumulate.
        repo = FakeChunkRepository()
        store = FakeVectorStore()
        service = make_service(repo, config, embeddings=FakeEmbeddingProvider(), vectors=store)
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        await service.index_document(document)  # type: ignore[arg-type]
        before = set(store.points)
        await service.index_document(document, force=True)  # type: ignore[arg-type]
        assert set(store.points) == before

    def test_the_point_id_is_a_pure_function_of_the_passage_ref(self) -> None:
        document_id = uuid.uuid4()
        ref = chunk_ref(document_id, 7)
        assert ref.endswith(":00007")
        assert point_id_for(ref) == point_id_for(ref)
        assert point_id_for(ref) != point_id_for(chunk_ref(document_id, 8))


class TestDegradation:
    async def test_no_embedding_provider_still_writes_passages(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        repo = FakeChunkRepository()
        service = make_service(repo, config)
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        outcome = await service.index_document(document)  # type: ignore[arg-type]

        # Skipped, not failed: the passages exist and are keyword-searchable.
        assert outcome.status == DocumentIndexStatus.SKIPPED
        assert outcome.chunks > 0
        assert outcome.embedded == 0
        assert all(row.vector_point_id is None for row in repo.rows[document.id])
        assert "keyword" in (document.index_error or "")

    async def test_a_document_with_no_text_is_skipped_not_failed(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        # An image with no OCR already carries an extraction status saying why; this is
        # not a second failure stacked on top of it.
        repo = FakeChunkRepository()
        service = make_service(repo, config, embeddings=FakeEmbeddingProvider())
        document = FakeDocument(
            fnol_case_id=uuid.uuid4(),
            extracted_text="",
            extraction_status=DocumentExtractionStatus.UNSUPPORTED,
            extraction_error="Images are stored as evidence; OCR is not enabled.",
        )

        outcome = await service.index_document(document)  # type: ignore[arg-type]
        assert outcome.status == DocumentIndexStatus.SKIPPED
        assert outcome.chunks == 0
        assert "OCR" in (outcome.error or "")

    async def test_disabled_config_does_nothing(self) -> None:
        repo = FakeChunkRepository()
        service = make_service(
            repo,
            DocumentIntelligenceSettings(enabled=False),
            embeddings=FakeEmbeddingProvider(),
        )
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        outcome = await service.index_document(document)  # type: ignore[arg-type]
        assert outcome.chunks == 0
        assert repo.replace_calls == 0

    async def test_a_document_indexed_before_a_provider_existed_is_not_current(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        # Chunked but not embedded, and now a provider exists. Treating that as
        # up-to-date is how a case ends up permanently half-indexed.
        repo = FakeChunkRepository()
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        await make_service(repo, config).index_document(document)  # type: ignore[arg-type]
        assert document.embedded_chunk_count == 0

        embeddings = FakeEmbeddingProvider()
        await make_service(
            repo, config, embeddings=embeddings, vectors=FakeVectorStore()
        ).index_document(document)  # type: ignore[arg-type]
        assert embeddings.document_calls == 1
        assert document.embedded_chunk_count == document.chunk_count


class TestFailureIsolation:
    async def test_a_retryable_provider_failure_is_recorded_and_re_raised(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        repo = FakeChunkRepository()
        service = make_service(
            repo,
            config,
            embeddings=FailingEmbeddingProvider(retryable=True),
            vectors=FakeVectorStore(),
        )
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        with pytest.raises(TransientIndexError):
            await service.index_document(document)  # type: ignore[arg-type]

        # Recorded before re-raising, so the retry is bounded rather than infinite.
        assert document.index_status == DocumentIndexStatus.FAILED
        assert document.index_attempts == 1
        assert document.index_error is not None

    async def test_a_permanent_failure_is_not_re_raised(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        repo = FakeChunkRepository()
        service = make_service(
            repo,
            config,
            embeddings=FailingEmbeddingProvider(retryable=False),
            vectors=FakeVectorStore(),
        )
        document = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text=LONG_TEXT)

        outcome = await service.index_document(document)  # type: ignore[arg-type]
        assert outcome.status == DocumentIndexStatus.FAILED
        assert document.index_status == DocumentIndexStatus.FAILED

    async def test_attempts_stop_at_the_limit(self, config: DocumentIntelligenceSettings) -> None:
        repo = FakeChunkRepository()
        service = make_service(repo, config, embeddings=FakeEmbeddingProvider())
        document = FakeDocument(
            fnol_case_id=uuid.uuid4(),
            extracted_text=LONG_TEXT,
            index_attempts=3,
            index_error="It failed three times.",
        )

        outcome = await service.index_document(document)  # type: ignore[arg-type]
        assert outcome.status == DocumentIndexStatus.FAILED
        assert repo.replace_calls == 0

    async def test_one_bad_document_does_not_lose_the_others(
        self, config: DocumentIntelligenceSettings
    ) -> None:
        repo = FakeChunkRepository()
        case_id = uuid.uuid4()
        service = make_service(
            repo, config, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
        )

        good_one = FakeDocument(fnol_case_id=case_id, extracted_text=LONG_TEXT)
        unreadable = FakeDocument(
            fnol_case_id=case_id, extracted_text="", extraction_error="A scan."
        )
        good_two = FakeDocument(
            fnol_case_id=case_id, extracted_text=LONG_TEXT, checksum_sha256="cafebabe"
        )

        outcome = await service.index_case([good_one, unreadable, good_two])  # type: ignore[arg-type]

        assert outcome.indexed == 2
        assert len(outcome.documents) == 3
        assert good_one.index_status == DocumentIndexStatus.INDEXED
        assert good_two.index_status == DocumentIndexStatus.INDEXED
        assert unreadable.index_status == DocumentIndexStatus.SKIPPED


class TestSignatures:
    def test_the_extraction_signature_follows_the_bytes_and_the_reader(self) -> None:
        one = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text="x", checksum_sha256="aaa")
        two = FakeDocument(fnol_case_id=uuid.uuid4(), extracted_text="x", checksum_sha256="bbb")
        assert extraction_signature(one) != extraction_signature(two)  # type: ignore[arg-type]

        # Same bytes, different reader — a text-layer read and an OCR read of the same
        # file are different answers and must not share a signature.
        three = FakeDocument(
            fnol_case_id=uuid.uuid4(),
            extracted_text="x",
            checksum_sha256="aaa",
            text_extractor="pdf_ocr",
        )
        assert extraction_signature(one) != extraction_signature(three)  # type: ignore[arg-type]

    def test_the_case_signature_ignores_document_order(self) -> None:
        case_id = uuid.uuid4()
        one = FakeDocument(
            fnol_case_id=case_id,
            extracted_text="a",
            checksum_sha256="aaa",
            extraction_signature="s1",
            index_fingerprint="f1",
        )
        two = FakeDocument(
            fnol_case_id=case_id,
            extracted_text="b",
            checksum_sha256="bbb",
            extraction_signature="s2",
            index_fingerprint="f2",
        )
        assert case_signature([one, two]) == case_signature([two, one])  # type: ignore[arg-type]

    def test_the_case_signature_moves_when_a_document_is_re_indexed(self) -> None:
        case_id = uuid.uuid4()
        before = FakeDocument(
            fnol_case_id=case_id,
            extracted_text="a",
            checksum_sha256="aaa",
            extraction_signature="s1",
            index_fingerprint="f1",
        )
        after = FakeDocument(
            fnol_case_id=case_id,
            extracted_text="a",
            checksum_sha256="aaa",
            extraction_signature="s1",
            index_fingerprint="f2",
        )
        # This is what makes the pipeline re-extract after a re-index rather than
        # keeping an extraction that cites passages which no longer exist.
        assert case_signature([before]) != case_signature([after])  # type: ignore[arg-type]
