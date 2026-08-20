"""What the policy library will accept, and what it refuses.

An upload endpoint that trusts the client stores whatever the client sends under
whatever name the client chose. These cases pin the three refusals that matter and the
one acceptance that has to keep working:

* **PDF only.** Not squeamishness about other formats — page geometry is only resolved
  for PDFs, so an excerpt from a `.docx` could never be shown on its page.
* **Refused before the bytes are stored.** A rejection that happens after the blob is
  written leaves a 40MB object in the bucket with no row pointing at it.
* **The same file twice is one library entry.** A duplicate wording would be retrieved
  against, scored and ranked as a rival candidate for the same contract.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import PolicyLibrarySettings
from app.core.errors import ConflictError
from app.domain.enums import DocumentExtractionStatus, PolicyIngestStatus
from app.services.documents.extracted import ExtractedPage
from app.services.documents.service import StoredDocument
from app.services.policies.library import PolicyLibraryService, PolicyUploadRejected

PDF_BYTES = b"%PDF-1.7\n" + b"policy wording body " * 40


class FakeDocumentRepository:
    """Rows in a dict, keyed by checksum the way the unique index keys them."""

    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.deleted: list[Any] = []

    async def flush(self) -> None:
        for row in self.rows:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    async def count(self) -> int:
        return len(self.rows)

    async def get_by_checksum(self, checksum: str) -> Any:
        return next((row for row in self.rows if row.checksum_sha256 == checksum), None)

    def add(self, document: Any) -> Any:
        self.rows.append(document)
        return document

    async def delete(self, document: Any) -> None:
        self.rows.remove(document)
        self.deleted.append(document)


class FakeProcessing:
    """Stands in for `DocumentProcessingService`, recording what it was asked to store."""

    def __init__(self, *, content_type: str = "application/pdf") -> None:
        self.content_type = content_type
        self.stored: list[tuple[str, int]] = []
        self.removed: list[str] = []
        #: Which top-level key namespace each call asked for.
        self.namespaces: list[str] = []

    async def process(
        self,
        *,
        owner_reference: str,
        filename: str,
        content: bytes,
        max_bytes: int,
        declared_content_type: str | None = None,
        namespace: str = "fnol",
    ) -> StoredDocument:
        del declared_content_type
        if len(content) > max_bytes:
            from app.services.documents.validation import DocumentValidationError

            raise DocumentValidationError("That file is larger than the limit.")

        self.namespaces.append(namespace)
        key = f"{namespace}/{owner_reference}/{filename}"
        self.stored.append((key, len(content)))
        return StoredDocument(
            filename=filename,
            content_type=self.content_type,
            size_bytes=len(content),
            checksum="c0ffee" * 10 + "abcd",
            storage_key=key,
            kind="document",
            text="POLICY NUMBER: CP-1000-11111\nNAMED INSURED: Northline Logistics Ltd\n",
            extraction_status=DocumentExtractionStatus.EXTRACTED,
            page_count=3,
            extraction_error=None,
            extractor="pdf_text_layer",
            pages=(ExtractedPage(number=1, text="page one", char_start=0, char_end=8),),
        )

    async def fetch(self, key: str) -> bytes:
        del key
        return PDF_BYTES

    async def remove(self, key: str) -> None:
        self.removed.append(key)


def build(
    *,
    documents: FakeDocumentRepository | None = None,
    processing: FakeProcessing | None = None,
    vectors: Any = None,
    config: PolicyLibrarySettings | None = None,
) -> tuple[PolicyLibraryService, FakeDocumentRepository, FakeProcessing]:
    repository = documents or FakeDocumentRepository()
    files = processing or FakeProcessing()
    service = PolicyLibraryService(
        repository,  # type: ignore[arg-type]
        files,  # type: ignore[arg-type]
        vectors=vectors,
        config=config or PolicyLibrarySettings(),
    )
    return service, repository, files


class TestAcceptance:
    async def test_a_pdf_is_accepted_and_left_pending(self) -> None:
        service, repository, files = build()

        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES, actor="Ada")

        assert receipt.duplicate is False
        assert receipt.needs_ingestion is True
        assert receipt.document.ingest_status == PolicyIngestStatus.PENDING
        assert receipt.document.uploaded_by == "Ada"
        assert len(repository.rows) == 1
        assert len(files.stored) == 1

    async def test_the_bytes_land_in_the_policy_namespace_not_the_claim_one(self) -> None:
        """A wording is reference data, not claim material.

        A bucket where the carrier's policy book sits under `fnol/` says those documents
        belong to a notice — which is the opposite of true, and they have a different
        retention and access story to match. Stored *once*, under the right prefix,
        rather than written and re-homed.
        """
        from app.services.policies.library import POLICY_NAMESPACE

        service, _, files = build()

        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        assert files.namespaces == [POLICY_NAMESPACE]
        assert len(files.stored) == 1
        assert receipt.document.storage_key.startswith(POLICY_NAMESPACE)
        assert not receipt.document.storage_key.startswith("fnol")

    async def test_the_declarations_text_is_stored_with_the_row(self) -> None:
        """Read on upload rather than in the worker.

        The text is the one part of ingestion that needs the bytes, so doing it here
        means a re-ingest after a chunk-size change never fetches from object storage.
        """
        service, _, _ = build()

        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        assert "CP-1000-11111" in (receipt.document.extracted_text or "")
        assert receipt.document.text_characters > 0
        assert receipt.document.page_count == 3
        assert receipt.document.text_extractor == "pdf_text_layer"

    async def test_marking_queued_only_moves_a_pending_row(self) -> None:
        service, _, _ = build()
        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        service.mark_queued(receipt.document)
        assert receipt.document.ingest_status == PolicyIngestStatus.QUEUED

        # An already-failed row must not be silently returned to the queue by a second
        # upload of a different file: only the worker and the reaper move it.
        receipt.document.ingest_status = PolicyIngestStatus.FAILED
        service.mark_queued(receipt.document)
        assert receipt.document.ingest_status == PolicyIngestStatus.FAILED


class TestRefusals:
    @pytest.mark.parametrize(
        "filename",
        ["wording.docx", "schedule.xlsx", "policy.txt", "archive.zip", "policy.PDF.exe"],
    )
    async def test_only_pdfs_are_accepted(self, filename: str) -> None:
        service, _, files = build()

        with pytest.raises(PolicyUploadRejected, match="PDF"):
            await service.upload(filename=filename, content=PDF_BYTES)

        # Refused *before* anything was stored. A rejection after the write leaves a
        # blob nothing has a row pointing at.
        assert files.stored == []

    async def test_a_file_over_the_limit_is_refused(self) -> None:
        service, _, _ = build(config=PolicyLibrarySettings(max_document_bytes=64))

        with pytest.raises(PolicyUploadRejected, match="larger than"):
            await service.upload(filename="policy.pdf", content=PDF_BYTES)

    async def test_a_non_pdf_content_type_is_refused_after_sniffing(self) -> None:
        """The extension said PDF and the bytes did not.

        The name check passes and the reader's own content-type resolution is what
        catches it, which is why the guard is in two places rather than one.
        """
        service, repository, _ = build(processing=FakeProcessing(content_type="text/plain"))

        with pytest.raises(PolicyUploadRejected, match="accepted as PDF"):
            await service.upload(filename="policy.pdf", content=PDF_BYTES)
        assert repository.rows == []

    async def test_a_full_library_is_refused_with_a_conflict(self) -> None:
        service, repository, _ = build(config=PolicyLibrarySettings(max_documents=1))
        await service.upload(filename="one.pdf", content=PDF_BYTES)
        assert len(repository.rows) == 1

        with pytest.raises(ConflictError, match="limited to"):
            await service.upload(filename="two.pdf", content=b"%PDF-1.7\nsomething else")

    async def test_uploads_are_refused_when_the_library_is_switched_off(self) -> None:
        """Refused rather than accepted and never ingested.

        An administrator who cannot upload knows the feature is off; one whose upload
        sits at `pending` forever does not.
        """
        service, _, _ = build(config=PolicyLibrarySettings(enabled=False))

        with pytest.raises(PolicyUploadRejected, match="switched off"):
            await service.upload(filename="policy.pdf", content=PDF_BYTES)


class TestIdempotency:
    async def test_the_same_bytes_twice_is_one_library_entry(self) -> None:
        service, repository, files = build()

        first = await service.upload(filename="policy.pdf", content=PDF_BYTES)
        second = await service.upload(filename="renamed.pdf", content=PDF_BYTES)

        assert second.duplicate is True
        assert second.document is first.document
        assert len(repository.rows) == 1
        # Nothing re-stored and nothing re-read: a retried request costs one query.
        assert len(files.stored) == 1

    async def test_a_duplicate_of_an_ingested_document_needs_no_ingestion(self) -> None:
        service, _, _ = build()
        first = await service.upload(filename="policy.pdf", content=PDF_BYTES)
        first.document.ingest_status = PolicyIngestStatus.EMBEDDED

        second = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        assert second.duplicate is True
        assert second.needs_ingestion is False


class TestRemoval:
    async def test_vectors_are_deleted_before_the_row(self) -> None:
        """The order is the point.

        Deleting the row first and then failing would leave points in the collection
        that nothing in Postgres can name again — so nothing could ever remove them.
        """
        order: list[str] = []

        class RecordingVectors:
            name = "recording"

            async def ensure_ready(self, *, dimension: int) -> None: ...

            async def upsert(self, records: Any) -> int:
                return 0

            async def delete_for_document(self, document_id: uuid.UUID) -> None:
                del document_id
                order.append("vectors")

            async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
                return []

            async def aclose(self) -> None: ...

        class RecordingRepository(FakeDocumentRepository):
            async def delete(self, document: Any) -> None:
                order.append("row")
                await super().delete(document)

        service, repository, files = build(
            documents=RecordingRepository(), vectors=RecordingVectors()
        )
        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        await service.remove(receipt.document)

        assert order == ["vectors", "row"]
        assert repository.rows == []
        assert files.removed  # the blob goes last, and its failure is tolerated

    async def test_a_vector_store_failure_leaves_the_row_in_place(self) -> None:
        """The order exists so that a failure here is retryable.

        Deleting the row first and then failing would leave points in the collection that
        nothing in Postgres can name again — so nothing could ever remove them. A vector
        delete that genuinely could not be done must therefore stop the removal.
        """

        class BrokenVectors:
            name = "broken"

            async def ensure_ready(self, *, dimension: int) -> None: ...

            async def upsert(self, records: Any) -> int:
                return 0

            async def delete_for_document(self, document_id: uuid.UUID) -> None:
                del document_id
                raise ConnectionError("the vector store is unreachable")

            async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
                return []

            async def aclose(self) -> None: ...

        service, repository, _ = build(vectors=BrokenVectors())
        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        with pytest.raises(ConnectionError):
            await service.remove(receipt.document)

        assert repository.rows == [receipt.document]
        assert repository.deleted == []

    async def test_a_failed_blob_delete_does_not_fail_the_removal(self) -> None:
        """A stranded blob costs disk. A row whose document cannot be opened costs trust."""

        class BrokenProcessing(FakeProcessing):
            async def remove(self, key: str) -> None:
                raise RuntimeError("the bucket is unreachable")

        service, repository, _ = build(processing=BrokenProcessing())
        receipt = await service.upload(filename="policy.pdf", content=PDF_BYTES)

        await service.remove(receipt.document)

        assert repository.rows == []
