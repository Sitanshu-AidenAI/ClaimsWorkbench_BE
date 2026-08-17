"""Indexing one document: read it, cut it into passages, vectorise them.

Two properties this service exists to guarantee, and everything in it serves one or
the other.

**Idempotent.** Running it twice on an unchanged document does one `SELECT` and
nothing else — no re-read, no embedding call, no vector write, no row written. The
guard is a fingerprint over everything the passages depend on: the file's checksum,
which reader read it, the chunk parameters, and the embedding model. A re-run after a
chunk-size change *does* redo the work, because the passages genuinely would be
different, and that is the same test.

**Isolated.** One document failing is one document failing. It never raises for a
document-level problem; it records the failure on the row and returns. A batch is a
loop of these, and the eleventh attachment being a corrupt PDF must not lose the ten
before it — which is also why each document's outcome is committed on its own, exactly
as `MailIntakeService` commits per message.

Failure classification is by **type**, not by reading exception messages. IIF's
`_is_transient_error` substring-matches on the text of an exception, which means a
document whose *content* mentions "timeout" is retried forever. `AIProviderError`
already carries `retryable`; everything else is transient only if it is a transport or
database error.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.core.config import DocumentIntelligenceSettings, settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.enums import DocumentIndexStatus
from app.models.fnol import FNOLDocument, FNOLDocumentChunk
from app.repositories.chunks import DocumentChunkRepository, chunk_ref
from app.services.ai.base import AIProviderError
from app.services.intelligence.chunking import Region, chunk_document, regions_from_pages
from app.services.intelligence.embedding import EmbeddingProvider
from app.services.intelligence.vectors import VectorRecord, VectorStore, point_id_for

logger = get_logger(__name__)


class TransientIndexError(Exception):
    """Worth retrying: the work was not done and could be next time."""


@dataclass(slots=True)
class DocumentIndexOutcome:
    """What happened to one document."""

    document_id: uuid.UUID
    status: DocumentIndexStatus
    chunks: int = 0
    embedded: int = 0
    reused: bool = False
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in (DocumentIndexStatus.INDEXED, DocumentIndexStatus.SKIPPED)


@dataclass(slots=True)
class CaseIndexOutcome:
    """What happened to a case's documents, and the signature of the result.

    `signature` is what the extraction fingerprint folds in, so a case whose passages
    have changed re-extracts and a case whose passages have not does not.
    """

    documents: list[DocumentIndexOutcome] = field(default_factory=list)
    signature: str = ""
    enabled: bool = True

    @property
    def indexed(self) -> int:
        return sum(1 for outcome in self.documents if outcome.status == DocumentIndexStatus.INDEXED)

    @property
    def failed(self) -> int:
        return sum(1 for outcome in self.documents if outcome.status == DocumentIndexStatus.FAILED)

    @property
    def chunks(self) -> int:
        return sum(outcome.chunks for outcome in self.documents)

    @property
    def embedded(self) -> int:
        return sum(outcome.embedded for outcome in self.documents)

    @classmethod
    def disabled(cls) -> CaseIndexOutcome:
        return cls(enabled=False)


class DocumentIndexService:
    """Turns stored documents into searchable, citable passages."""

    def __init__(
        self,
        chunks: DocumentChunkRepository,
        *,
        embeddings: EmbeddingProvider | None = None,
        vectors: VectorStore | None = None,
        config: DocumentIntelligenceSettings | None = None,
    ) -> None:
        self._chunks = chunks
        self._embeddings = embeddings
        self._vectors = vectors
        self._config = config or settings.docint

    # -- Public surface ------------------------------------------------------

    async def index_case(
        self, documents: list[FNOLDocument], *, force: bool = False
    ) -> CaseIndexOutcome:
        """Index every document on a case, one at a time, isolating failures."""
        if not self._config.enabled:
            return CaseIndexOutcome.disabled()

        outcome = CaseIndexOutcome()
        for document in documents:
            outcome.documents.append(await self.index_document(document, force=force))
        outcome.signature = case_signature(documents)
        return outcome

    async def index_document(
        self, document: FNOLDocument, *, force: bool = False
    ) -> DocumentIndexOutcome:
        """Index one document. Never raises for a document-level problem."""
        if not self._config.enabled:
            return DocumentIndexOutcome(
                document_id=document.id, status=DocumentIndexStatus(document.index_status)
            )

        if not (document.extracted_text or "").strip():
            # Nothing to cut into passages. An image with no OCR, a scan, a legacy
            # `.doc` — all already carry an extraction status saying why, and this is
            # not a second failure on top of it.
            return self._settle(
                document,
                status=DocumentIndexStatus.SKIPPED,
                error=document.extraction_error or "This document has no text to index.",
            )

        fingerprint = self.fingerprint(document)
        if not force and self._is_current(document, fingerprint):
            return DocumentIndexOutcome(
                document_id=document.id,
                status=DocumentIndexStatus(document.index_status),
                chunks=document.chunk_count,
                embedded=document.embedded_chunk_count,
                reused=True,
            )

        if document.index_attempts >= self._config.index_max_attempts and not force:
            return DocumentIndexOutcome(
                document_id=document.id,
                status=DocumentIndexStatus.FAILED,
                error=document.index_error
                or f"Indexing was abandoned after {document.index_attempts} attempts.",
            )

        document.index_status = DocumentIndexStatus.INDEXING
        document.index_attempts += 1
        document.index_error = None
        await self._chunks.flush()

        try:
            return await self._run(document, fingerprint)
        except Exception as exc:
            transient = _is_transient(exc)
            logger.warning(
                "document_index_failed",
                document_id=str(document.id),
                error=type(exc).__name__,
                transient=transient,
            )
            outcome = self._settle(
                document,
                status=DocumentIndexStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                fingerprint=fingerprint if not transient else None,
            )
            if transient:
                # Re-raised so the worker can schedule a retry. The row already
                # records the failure, so nothing is lost if the retry never happens.
                raise TransientIndexError(str(exc)) from exc
            return outcome

    # -- Fingerprints --------------------------------------------------------

    def fingerprint(self, document: FNOLDocument) -> str:
        """Everything this document's passages depend on, hashed."""
        material = "|".join(
            (
                document.extraction_signature or extraction_signature(document),
                self._config.index_signature,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _is_current(self, document: FNOLDocument, fingerprint: str) -> bool:
        """Whether the stored passages already match this fingerprint.

        The vector count is part of the test: a document chunked but not embedded
        because the provider was down is *not* current once a provider exists, and
        treating it as current is how a case ends up permanently half-indexed.
        """
        if document.index_fingerprint != fingerprint:
            return False
        if document.index_status not in (
            DocumentIndexStatus.INDEXED,
            DocumentIndexStatus.SKIPPED,
        ):
            return False
        if self._can_embed and document.embedded_chunk_count < document.chunk_count:
            return False
        return document.chunk_count > 0

    @property
    def _can_embed(self) -> bool:
        return self._embeddings is not None and self._vectors is not None

    # -- The work -----------------------------------------------------------

    async def _run(self, document: FNOLDocument, fingerprint: str) -> DocumentIndexOutcome:
        text = document.extracted_text or ""
        regions = self._regions(document, text)

        chunks, truncated = chunk_document(
            text,
            regions=regions,
            target_chars=self._config.chunk_tokens * 4,
            overlap_chars=self._config.chunk_overlap_tokens * 4,
            max_chunks=self._config.max_chunks_per_document,
        )
        if not chunks:
            return self._settle(
                document,
                status=DocumentIndexStatus.SKIPPED,
                error="No passages could be cut from this document's text.",
                fingerprint=fingerprint,
            )

        rows = await self._chunks.replace_for_document(document, chunks)
        if truncated:
            logger.info(
                "document_chunks_truncated",
                document_id=str(document.id),
                limit=self._config.max_chunks_per_document,
            )

        embedded = 0
        embeddings, vectors = self._embeddings, self._vectors
        if embeddings is not None and vectors is not None:
            embedded = await self._vectorise(
                document, list(rows), embeddings=embeddings, vectors=vectors
            )
        else:
            # The passages exist and are keyword-searchable. Nothing is wrong here.
            await self._chunks.clear_vectors_for_document(document.id)

        status = DocumentIndexStatus.INDEXED if self._can_embed else DocumentIndexStatus.SKIPPED
        return self._settle(
            document,
            status=status,
            fingerprint=fingerprint,
            chunks=len(rows),
            embedded=embedded,
            error=None
            if self._can_embed
            else "Embeddings are not configured; passages are searchable by keyword only.",
        )

    def _regions(self, document: FNOLDocument, text: str) -> list[Region]:
        """The page or section spans passages may not be cut across."""
        offsets = document.page_offsets
        if not offsets:
            return []
        regions = regions_from_pages(offsets)
        # A reader that recorded offsets past the stored text is a reader whose text
        # was truncated after the fact. Clamp rather than trust, or a chunk's offsets
        # address text that is not there.
        return [
            Region(
                number=region.number,
                label=region.label,
                start=min(region.start, len(text)),
                end=min(region.end, len(text)),
            )
            for region in regions
            if region.start < len(text)
        ]

    async def _vectorise(
        self,
        document: FNOLDocument,
        rows: list[FNOLDocumentChunk],
        *,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
    ) -> int:
        """Embed the passages and write them to the index.

        Deletes the document's existing points first. Passage boundaries move when the
        chunker changes, so a document that now yields five passages where it yielded
        eight would otherwise leave three orphans in the index — findable, citable, and
        pointing at offsets that no longer mean anything.
        """
        await vectors.ensure_ready(dimension=embeddings.dimension)
        await vectors.delete_for_document(document.id)

        computed = await embeddings.embed_documents([row.content for row in rows])

        records = [
            VectorRecord(
                chunk_ref=row.chunk_ref,
                case_id=document.fnol_case_id,
                document_id=document.id,
                vector=vector,
                content_hash=row.content_hash,
                page_number=row.page_number,
                section_label=row.section_label,
            )
            # `strict=True`: the provider contract promises one vector per passage and
            # verifies it, so a mismatch here is a bug rather than a data condition —
            # and zipping short would mis-attribute every passage after the gap.
            for row, vector in zip(rows, computed, strict=True)
        ]
        written = await vectors.upsert(records)

        await self._chunks.mark_embedded(
            rows,
            point_ids={record.chunk_ref: point_id_for(record.chunk_ref) for record in records},
            model=embeddings.model,
            dimension=embeddings.dimension,
        )
        return written

    def _settle(
        self,
        document: FNOLDocument,
        *,
        status: DocumentIndexStatus,
        error: str | None = None,
        fingerprint: str | None = None,
        chunks: int | None = None,
        embedded: int | None = None,
    ) -> DocumentIndexOutcome:
        """Write the outcome onto the document row."""
        document.index_status = status
        document.index_error = error
        if fingerprint is not None:
            document.index_fingerprint = fingerprint
        if chunks is not None:
            document.chunk_count = chunks
        if embedded is not None:
            document.embedded_chunk_count = embedded
        if status in (DocumentIndexStatus.INDEXED, DocumentIndexStatus.SKIPPED):
            document.indexed_at = datetime.now(UTC)

        return DocumentIndexOutcome(
            document_id=document.id,
            status=status,
            chunks=document.chunk_count,
            embedded=document.embedded_chunk_count,
            error=error,
        )


def extraction_signature(document: FNOLDocument) -> str:
    """What the document's *text* depends on: its bytes and which reader read them."""
    material = "|".join(
        (
            document.checksum_sha256 or "",
            document.text_extractor or "unknown",
            document.text_extractor_version or "0",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def case_signature(documents: list[FNOLDocument]) -> str:
    """A case's document set, hashed by identity rather than by content.

    Folded into the extraction fingerprint. Hashing identity rather than text is both
    cheaper and stronger: the checksum identifies the bytes exactly, and the
    signatures identify how they were read and indexed — whereas hashing megabytes of
    text on every reuse check costs real time to answer the same question less well.
    Sorted, so the order documents were attached in cannot move the answer.
    """
    digest = hashlib.sha256()
    for part in sorted(
        "|".join(
            (
                document.checksum_sha256 or "",
                document.extraction_signature or "",
                document.index_fingerprint or "",
            )
        )
        for document in documents
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _is_transient(exc: Exception) -> bool:
    """Whether retrying could plausibly succeed.

    By type, never by reading the exception's message. A substring matcher — which is
    what the IIF pipeline uses — retries a document forever because the *document*
    mentions the word "timeout".
    """
    if isinstance(exc, AIProviderError):
        return exc.retryable
    if isinstance(exc, ExternalServiceError | httpx.TransportError | TransientIndexError):
        return True

    from sqlalchemy.exc import OperationalError

    return isinstance(exc, OperationalError)


__all__ = [
    "CaseIndexOutcome",
    "DocumentIndexOutcome",
    "DocumentIndexService",
    "TransientIndexError",
    "case_signature",
    "chunk_ref",
    "extraction_signature",
]
