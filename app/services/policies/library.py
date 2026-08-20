"""Accepting a policy wording, and removing one.

The thin half of the library: validate, store, write a row, and hand the work to a
worker. Everything that takes time — reading a forty-page PDF, embedding a few
hundred passages — happens in `PolicyIngestionService`, off the request.

**Why the upload does not ingest inline.** The same argument the FNOL document route
makes, only more so: a policy is longer than a loss notice, so reading, chunking and
embedding one is seconds to minutes, inside an HTTP request that a proxy will give up
on first. The endpoint returns as soon as the bytes are safe and the row exists; the
client watches `ingest_status`. That is also what makes a bulk load of a carrier's
book possible at all — forty POSTs that each return in milliseconds, and a worker
that grinds through them.

**Validation is not a policy of its own.** `app/services/documents/validation.py`
already refuses anything executable, archived or scriptable and checks magic bytes
against the extension. This module narrows that allow-list to PDF and adds nothing
else, because a second validator is a second thing to keep in step with the first.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass

from app.core.config import PolicyLibrarySettings, settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domain.enums import DocumentExtractionStatus, PolicyIngestStatus
from app.models.policy_document import PolicyDocument
from app.repositories.policy_document import PolicyDocumentRepository
from app.services.documents.service import DocumentProcessingService, StoredDocument
from app.services.documents.validation import DocumentValidationError, sanitise_filename
from app.services.policies.vectors import PolicyVectorStore

logger = get_logger(__name__)

#: The only content type the library accepts.
#:
#: Narrower than `ALLOWED_EXTENSIONS`, and the reason is not squeamishness about
#: other formats — it is that `app/services/extraction/locate.py` resolves page
#: geometry only for `application/pdf`. An excerpt quoted from a `.docx` could be
#: shown as text and never shown *on its page*, and "here is the clause, on page 14,
#: highlighted" is the whole value of quoting it. A carrier that has its wordings in
#: Word converts them once rather than losing the citation forever.
ACCEPTED_CONTENT_TYPES = frozenset({"application/pdf"})
ACCEPTED_EXTENSIONS = frozenset({".pdf"})


class PolicyUploadRejected(ValidationError):
    """The upload was refused. Carries a sentence fit to show an administrator."""

    code = "policy_document_rejected"


@dataclass(slots=True)
class UploadReceipt:
    """What one upload produced.

    `duplicate` is a success, not an error. Re-uploading a wording the library
    already holds returns the existing entry, so a retried request — or an
    administrator who lost track of what they had loaded — costs nothing and creates
    no rival candidate for the same contract.
    """

    document: PolicyDocument
    duplicate: bool = False

    @property
    def needs_ingestion(self) -> bool:
        return self.document.ingest_status in (
            PolicyIngestStatus.PENDING,
            PolicyIngestStatus.QUEUED,
        )


class PolicyLibraryService:
    def __init__(
        self,
        documents: PolicyDocumentRepository,
        processing: DocumentProcessingService,
        *,
        vectors: PolicyVectorStore | None = None,
        config: PolicyLibrarySettings | None = None,
    ) -> None:
        self._documents = documents
        self._processing = processing
        self._vectors = vectors
        self._config = config or settings.policy_library

    async def upload(
        self,
        *,
        filename: str,
        content: bytes,
        declared_content_type: str | None = None,
        actor: str | None = None,
    ) -> UploadReceipt:
        """Validate a wording, store its bytes, and write the row a worker will read.

        Text extraction happens *here* rather than in the worker, and that is a
        deliberate split from the FNOL path. Reading the text is the one part of
        ingestion that needs the bytes, and doing it now means the worker never has to
        fetch from object storage — so a re-ingest after a chunk-size change is a
        Postgres-only operation, and an object store that is briefly unreachable
        cannot fail a re-index. The expensive parts, chunking and embedding, stay in
        the worker.
        """
        if not self._config.enabled:
            raise PolicyUploadRejected(
                "Policy ingestion is switched off in this environment. "
                "Set CWB_POLICY_ENABLED=true to accept policy documents."
            )

        self._guard_type(filename)
        if await self._documents.count() >= self._config.max_documents:
            raise ConflictError(
                f"The policy library is limited to {self._config.max_documents} documents.",
                code="policy_library_full",
            )

        checksum = hashlib.sha256(content).hexdigest()
        existing = await self._documents.get_by_checksum(checksum)
        if existing is not None:
            logger.info(
                "policy_document_upload_duplicate",
                document_id=str(existing.id),
                filename=existing.filename,
            )
            return UploadReceipt(document=existing, duplicate=True)

        try:
            stored: StoredDocument = await self._processing.process(
                owner_reference=_owner_reference(checksum),
                filename=filename,
                content=content,
                max_bytes=self._config.max_document_bytes,
                declared_content_type=declared_content_type,
                # Its own top-level namespace, so the library can be listed, backed up
                # and retained separately from claim documents — and so a bucket does
                # not say the carrier's policy book belongs to a notice.
                namespace=POLICY_NAMESPACE,
            )
        except DocumentValidationError as exc:
            # Re-raised under this module's own code so the client can tell a
            # library rejection from a claim-attachment rejection, with the
            # validator's own sentence preserved.
            raise PolicyUploadRejected(exc.message) from exc

        if stored.content_type not in ACCEPTED_CONTENT_TYPES:
            raise PolicyUploadRejected(
                f"{stored.filename} is a {stored.content_type} file. "
                "Policy documents are accepted as PDF so that a quoted clause can be "
                "shown on its page."
            )

        document = PolicyDocument(
            filename=stored.filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.storage_key,
            # The checksum the duplicate check used, not the one the processor
            # returned. In production they are the same sha256 of the same bytes, and
            # keeping two sources for one unique key is how a library ends up with a
            # row whose checksum the duplicate check will never find again.
            checksum_sha256=checksum,
            uploaded_by=actor,
            extraction_status=stored.extraction_status,
            extracted_text=stored.text or None,
            text_characters=len(stored.text or ""),
            page_count=stored.page_count,
            extraction_error=stored.extraction_error,
            text_extractor=stored.extractor,
            text_extractor_version=_extractor_version(stored),
            page_offsets=stored.page_offsets or None,
            extracted_metadata={},
            ingest_status=PolicyIngestStatus.PENDING,
        )
        self._documents.add(document)
        await self._documents.flush()

        logger.info(
            "policy_document_uploaded",
            document_id=str(document.id),
            filename=document.filename,
            size_bytes=document.size_bytes,
            page_count=document.page_count,
            extraction_status=document.extraction_status,
            actor=actor,
        )
        return UploadReceipt(document=document)

    def mark_queued(self, document: PolicyDocument) -> None:
        """Record that a worker has been asked for this document.

        A state between "accepted" and "being read", so the Policies screen can say
        "queued" rather than leaving a row at `pending` and letting an administrator
        wonder whether anything is going to happen.
        """
        if document.ingest_status == PolicyIngestStatus.PENDING:
            document.ingest_status = PolicyIngestStatus.QUEUED

    async def get(self, document_id: uuid.UUID) -> PolicyDocument:
        document = await self._documents.get(document_id)
        if document is None:
            raise NotFoundError("That policy document could not be found.")
        return document

    async def content(self, document: PolicyDocument) -> bytes:
        return await self._processing.fetch(document.storage_key)

    async def remove(self, document: PolicyDocument) -> None:
        """Delete a wording from the library, from every store that holds it.

        Ordered deliberately: **vectors first, then the row.** A failed vector delete
        leaves the row in place and the operation retryable; deleting the row first
        and then failing would leave points in the collection that nothing in
        Postgres can ever name again, so nothing could ever remove them — the exact
        orphan-point failure `delete_for_document` exists to prevent.

        The object-storage delete is last and its failure is tolerated: a stranded
        blob costs disk, and a blob deleted before the row would give an
        administrator a library entry whose document cannot be opened.
        """
        if self._vectors is not None:
            await self._vectors.delete_for_document(document.id)

        key, document_id, filename = document.storage_key, document.id, document.filename
        await self._documents.delete(document)

        try:
            await self._processing.remove(key)
        except Exception as exc:
            logger.warning(
                "policy_document_blob_orphaned",
                document_id=str(document_id),
                error=type(exc).__name__,
            )

        logger.info("policy_document_removed", document_id=str(document_id), filename=filename)

    # -- Internals -----------------------------------------------------------

    def _guard_type(self, filename: str) -> None:
        """Refuse a non-PDF by name before the bytes are stored or parsed.

        Before, not after: the alternative is storing a 40MB `.zip` in the bucket and
        then telling the client it was refused, which leaves a blob behind that
        nothing has a row pointing at.
        """
        safe = sanitise_filename(filename, fallback="policy")
        extension = os.path.splitext(safe)[1].lower()
        if extension not in ACCEPTED_EXTENSIONS:
            raise PolicyUploadRejected(
                f"{safe} is not a PDF. Policy documents are accepted as PDF so that a "
                "quoted clause can be shown on its page."
            )


#: The top-level key namespace for policy wordings. A wording is not claim material:
#: a bucket where the carrier's policy book sits under `fnol/` says these documents
#: belong to a notice, which is exactly the thing this module's tables were separated
#: to stop being true. It is also what lets the library be backed up and retained on
#: its own schedule.
POLICY_NAMESPACE = "policy-library"


def _owner_reference(checksum: str) -> str:
    """The key shard for a wording, inside the policy namespace.

    Sharded by the checksum's first byte rather than by policy number: the number is
    not known until the document has been read, and a key that changed once ingestion
    identified the policy would be a key that no longer points at the stored bytes.
    """
    return checksum[:2]


def _extractor_version(stored: StoredDocument) -> str | None:
    """The reader's version, for the ingest fingerprint.

    Taken from the PDF reader's module constant rather than invented here, so a
    reader improvement invalidates every fingerprint exactly once and re-ingests the
    library rather than leaving half of it read by the old one.
    """
    if stored.extractor is None:
        return None
    from app.services.documents.formats import pdf

    return pdf.EXTRACTOR_VERSION if stored.extractor == pdf.EXTRACTOR else "1"


def is_readable(document: PolicyDocument) -> bool:
    """Whether text came out of this document at all."""
    return document.extraction_status == DocumentExtractionStatus.EXTRACTED and bool(
        (document.extracted_text or "").strip()
    )


__all__ = [
    "ACCEPTED_CONTENT_TYPES",
    "ACCEPTED_EXTENSIONS",
    "POLICY_NAMESPACE",
    "PolicyLibraryService",
    "PolicyUploadRejected",
    "UploadReceipt",
    "is_readable",
]
