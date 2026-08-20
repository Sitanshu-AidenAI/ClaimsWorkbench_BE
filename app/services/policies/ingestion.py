"""Ingesting one policy wording: read it, cut it, vectorise it, name it.

Two properties this service exists to guarantee, and everything in it serves one or
the other. They are the same two `DocumentIndexService` guarantees for claim
attachments, because they are the properties that make asynchronous ingestion safe
rather than merely asynchronous.

**Idempotent.** Running it twice on an unchanged document does one `SELECT` and
nothing else — no re-read, no embedding call, no vector write, no row rewritten. The
guard is a fingerprint over everything the passages depend on: the file's checksum,
which reader read it, the chunk parameters, and the embedding model. A re-run after a
chunk-size change *does* redo the work, because the passages genuinely would be
different, and that is the same test rather than an exception to it.

**Isolated.** One document failing is one document failing. This never raises for a
document-level problem; it records the failure on the row and returns, so a bulk load
of forty wordings does not lose thirty-nine to a corrupt PDF at position eleven.
Failure classification is by **exception type**, never by reading messages: a policy
whose own text contains the word "timeout" must not be retried forever.

One thing this does that the claim-side service does not: **it reads the document's
own declarations and links it to the book.** A wording's policy number, insured,
producer and term are what a match is filtered and explained with, and they are read
once here rather than on every match — the difference between a match costing a
database query and costing a re-parse of every PDF in the library.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.core.config import (
    DocumentIntelligenceSettings,
    PolicyLibrarySettings,
    settings,
)
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.enums import DocumentExtractionStatus, PolicyIngestStatus
from app.domain.policy_extraction import PolicyFacts, read_policy_facts
from app.models.policy_document import PolicyDocument, PolicyDocumentChunk
from app.repositories.policy import PolicyRepository
from app.repositories.policy_document import PolicyDocumentRepository
from app.services.ai.base import AIProviderError
from app.services.intelligence.chunking import Region, chunk_document, regions_from_pages
from app.services.intelligence.embedding import EmbeddingProvider
from app.services.policies.vectors import (
    PolicyVectorRecord,
    PolicyVectorStore,
    point_id_for,
)

logger = get_logger(__name__)


class TransientIngestError(Exception):
    """Worth retrying: the work was not done and could be next time."""


@dataclass(slots=True)
class IngestOutcome:
    """What happened to one policy document."""

    document_id: uuid.UUID
    status: PolicyIngestStatus
    chunks: int = 0
    embedded: int = 0
    reused: bool = False
    linked_policy_id: uuid.UUID | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in (PolicyIngestStatus.CHUNKED, PolicyIngestStatus.EMBEDDED)


class PolicyIngestionService:
    """Turns an uploaded wording into searchable, citable, matchable passages."""

    def __init__(
        self,
        documents: PolicyDocumentRepository,
        *,
        policies: PolicyRepository | None = None,
        embeddings: EmbeddingProvider | None = None,
        vectors: PolicyVectorStore | None = None,
        config: PolicyLibrarySettings | None = None,
        embedding_config: DocumentIntelligenceSettings | None = None,
    ) -> None:
        self._documents = documents
        self._policies = policies
        self._embeddings = embeddings
        self._vectors = vectors
        self._config = config or settings.policy_library
        self._embedding_config = embedding_config or settings.docint

    # -- Public surface ------------------------------------------------------

    async def ingest(self, document: PolicyDocument, *, force: bool = False) -> IngestOutcome:
        """Ingest one wording. Never raises for a document-level problem.

        The exception is a *transient* failure, which is re-raised after the row has
        been updated so the worker can schedule a retry. The row recording the
        attempt first is what makes that retry bounded rather than infinite.
        """
        if not self._config.enabled:
            return IngestOutcome(
                document_id=document.id, status=PolicyIngestStatus(document.ingest_status)
            )

        if not (document.extracted_text or "").strip():
            # A scan with no text layer. It already carries an extraction status
            # saying so, and this is not a second failure on top of the first — but
            # it *is* a failure here rather than a skip, because unlike a claim
            # attachment a wording that yielded no text is a policy the matcher can
            # never find. An administrator has to see it in red.
            return self._settle(
                document,
                status=PolicyIngestStatus.FAILED,
                error=document.extraction_error
                or "No text could be read from this PDF. It may be a scan needing OCR.",
            )

        fingerprint = self.fingerprint(document)
        if not force and self._is_current(document, fingerprint):
            return IngestOutcome(
                document_id=document.id,
                status=PolicyIngestStatus(document.ingest_status),
                chunks=document.chunk_count,
                embedded=document.embedded_chunk_count,
                reused=True,
                linked_policy_id=document.policy_id,
            )

        if document.ingest_attempts >= self._config.ingest_max_attempts and not force:
            return IngestOutcome(
                document_id=document.id,
                status=PolicyIngestStatus.FAILED,
                error=document.ingest_error
                or f"Ingestion was abandoned after {document.ingest_attempts} attempts.",
            )

        document.ingest_status = PolicyIngestStatus.EXTRACTING
        document.ingest_started_at = datetime.now(UTC)
        document.ingest_attempts += 1
        document.ingest_error = None
        await self._documents.flush()

        try:
            return await self._run(document, fingerprint)
        except Exception as exc:
            transient = _is_transient(exc)
            logger.warning(
                "policy_document_ingest_failed",
                document_id=str(document.id),
                error=type(exc).__name__,
                transient=transient,
            )
            outcome = self._settle(
                document,
                status=PolicyIngestStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                # A transient failure must not stamp the fingerprint, or the retry
                # would look like a re-run of work already done and skip itself.
                fingerprint=None if transient else fingerprint,
            )
            if transient:
                raise TransientIngestError(str(exc)) from exc
            return outcome

    # -- Fingerprints --------------------------------------------------------

    def fingerprint(self, document: PolicyDocument) -> str:
        """Everything this document's passages depend on, hashed.

        The embedding model is in here and the *vector store* is not, deliberately:
        changing the model changes the vectors and must re-embed, whereas moving Qdrant
        changes where identical vectors live and the point ids make that a clean overwrite.

        The model is read from the **provider**, not from configuration, and that is a
        correction rather than a detail. A fingerprint exists to describe what actually
        produced the stored vectors; taking the configured *name* instead means any
        deployment where the two diverge — a gateway aliasing one model to another, a
        stub, a plain misconfiguration — computes a fingerprint that matches, treats a
        stale index as current, and never re-embeds. The failure is silent and permanent,
        which is the worst shape a caching bug can take.
        """
        embeddings = self._embeddings
        if self._can_embed and embeddings is not None:
            model, dimension = embeddings.model, embeddings.dimension
        else:
            model, dimension = "none", 0

        material = "|".join(
            (
                document.checksum_sha256 or "",
                document.text_extractor or "unknown",
                document.text_extractor_version or "0",
                self._config.chunk_signature,
                model,
                str(dimension),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _is_current(self, document: PolicyDocument, fingerprint: str) -> bool:
        """Whether the stored passages already match this fingerprint.

        The vector count is part of the test: a document chunked but not embedded
        because the provider was down is *not* current once a provider exists, and
        treating it as current is how a library ends up permanently half-indexed and
        matching on half of itself.
        """
        if document.ingest_fingerprint != fingerprint:
            return False
        if document.ingest_status not in (
            PolicyIngestStatus.CHUNKED,
            PolicyIngestStatus.EMBEDDED,
        ):
            return False
        if self._can_embed and document.embedded_chunk_count < document.chunk_count:
            return False
        return document.chunk_count > 0

    @property
    def _can_embed(self) -> bool:
        return self._embeddings is not None and self._vectors is not None

    # -- The work -----------------------------------------------------------

    async def _run(self, document: PolicyDocument, fingerprint: str) -> IngestOutcome:
        text = document.extracted_text or ""

        # Read the declarations first. Even a document that yields no cuttable
        # passages should carry its own policy number on the row: an administrator
        # looking at a failed ingestion needs to know *which policy* failed.
        facts = read_policy_facts(text, filename=document.filename)
        self._apply(document, facts)
        linked = await self._link(document, facts)

        chunks, truncated = chunk_document(
            text,
            regions=self._regions(document, text),
            target_chars=self._config.chunk_tokens * 4,
            overlap_chars=self._config.chunk_overlap_tokens * 4,
            max_chunks=self._config.max_chunks_per_document,
        )
        if not chunks:
            return self._settle(
                document,
                status=PolicyIngestStatus.FAILED,
                error="No passages could be cut from this document's text.",
                fingerprint=fingerprint,
            )

        rows = await self._documents.replace_chunks_for_document(document, chunks)
        if truncated:
            logger.info(
                "policy_document_chunks_truncated",
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
            # The passages exist and are keyword-searchable. Nothing is wrong here,
            # and `CHUNKED` is the settled state that says so.
            await self._documents.clear_vectors_for_document(document.id)

        status = PolicyIngestStatus.EMBEDDED if self._can_embed else PolicyIngestStatus.CHUNKED
        outcome = self._settle(
            document,
            status=status,
            fingerprint=fingerprint,
            chunks=len(rows),
            embedded=embedded,
            error=None
            if self._can_embed
            else "No embedding provider is configured; this wording is searchable by keyword only.",
        )
        outcome.linked_policy_id = linked

        logger.info(
            "policy_document_ingested",
            document_id=str(document.id),
            policy_number=document.policy_number,
            chunks=len(rows),
            embedded=embedded,
            linked=bool(linked),
        )
        return outcome

    def _regions(self, document: PolicyDocument, text: str) -> list[Region]:
        """The page spans passages may not be cut across.

        A policy's page boundaries are meaningful in a way a loss notice's are not —
        a schedule of premises is a page, an endorsement is a page — so honouring
        them makes an excerpt's page number exact *and* makes the passage a coherent
        unit rather than a window over two unrelated clauses.
        """
        offsets = document.page_offsets
        if not offsets:
            return []
        # A reader that recorded offsets past the stored text is a reader whose text
        # was truncated after the fact. Clamp rather than trust, or an excerpt's
        # offsets address text that is not there.
        return [
            Region(
                number=region.number,
                label=region.label,
                start=min(region.start, len(text)),
                end=min(region.end, len(text)),
            )
            for region in regions_from_pages(offsets)
            if region.start < len(text)
        ]

    async def _vectorise(
        self,
        document: PolicyDocument,
        rows: list[PolicyDocumentChunk],
        *,
        embeddings: EmbeddingProvider,
        vectors: PolicyVectorStore,
    ) -> int:
        """Embed the passages and write them to the policy collection.

        Deletes the document's existing points first. Passage boundaries move when the
        chunker changes, so a wording that now yields forty passages where it yielded
        sixty would otherwise leave twenty orphans in the collection — findable,
        quotable, and pointing at offsets that no longer mean anything.
        """
        await vectors.ensure_ready(dimension=embeddings.dimension)
        await vectors.delete_for_document(document.id)

        computed = await embeddings.embed_documents([row.content for row in rows])

        records = [
            PolicyVectorRecord(
                chunk_ref=row.chunk_ref,
                document_id=document.id,
                vector=vector,
                content_hash=row.content_hash,
                policy_id=document.policy_id,
                policy_number=document.policy_number,
                line_of_business=document.line_of_business,
                page_number=row.page_number,
                section_label=row.section_label,
                effective_date=document.effective_date,
                expiry_date=document.expiry_date,
            )
            # `strict=True`: the provider contract promises one vector per passage
            # and verifies it, so a mismatch here is a bug rather than a data
            # condition — and zipping short would mis-attribute every passage after
            # the gap, which is a citation pointing at the wrong clause with nothing
            # anywhere to say that anything went wrong.
            for row, vector in zip(rows, computed, strict=True)
        ]
        written = await vectors.upsert(records)

        await self._documents.mark_embedded(
            rows,
            point_ids={record.chunk_ref: point_id_for(record.chunk_ref) for record in records},
            model=embeddings.model,
            dimension=embeddings.dimension,
        )
        return written

    # -- Facts and linkage ---------------------------------------------------

    def _apply(self, document: PolicyDocument, facts: PolicyFacts) -> None:
        """Land what the document says about itself onto the row.

        Read values never overwrite a value already on the row with a *different*
        provenance — but nothing sets these except this method today, so the rule is
        simply "last read wins", and re-ingesting after an extractor improvement is
        expected to move them. That is why `ingest_fingerprint` includes the
        extractor version: the row's facts and its passages are re-derived together
        or not at all.
        """
        document.policy_number = facts.policy_number
        document.insured_name = facts.insured_name
        document.insurer_name = facts.insurer_name
        document.broker_name = facts.broker_name
        document.policy_type = facts.policy_type
        document.line_of_business = facts.line_of_business.value if facts.line_of_business else None
        document.effective_date = facts.effective_date
        document.expiry_date = facts.expiry_date
        document.extracted_metadata = facts.as_metadata

    async def _link(self, document: PolicyDocument, facts: PolicyFacts) -> uuid.UUID | None:
        """Link the wording to its book row, when the number resolves to one.

        **The safety rule, restated for this path:** nothing here *creates* a policy.
        The number read out of a PDF is looked up in the book, and a number that
        matches nothing leaves the document unlinked — which is a first-class state,
        not a failure. A wording that invented a policy row would let a mis-read
        digit put a claim on a contract that does not exist.
        """
        if self._policies is None or not facts.policy_number:
            return document.policy_id

        found = await self._policies.get_by_number(facts.policy_number)
        policy_id = found.id if found is not None else None
        if policy_id != document.policy_id:
            await self._documents.link_policy(document, policy_id)
            logger.info(
                "policy_document_linked" if policy_id else "policy_document_unlinked",
                document_id=str(document.id),
                policy_number=facts.policy_number,
            )
        return policy_id

    def _settle(
        self,
        document: PolicyDocument,
        *,
        status: PolicyIngestStatus,
        error: str | None = None,
        fingerprint: str | None = None,
        chunks: int | None = None,
        embedded: int | None = None,
    ) -> IngestOutcome:
        """Write the outcome onto the document row."""
        document.ingest_status = status
        document.ingest_error = error
        if fingerprint is not None:
            document.ingest_fingerprint = fingerprint
        if chunks is not None:
            document.chunk_count = chunks
        if embedded is not None:
            document.embedded_chunk_count = embedded
        if status in (PolicyIngestStatus.CHUNKED, PolicyIngestStatus.EMBEDDED):
            document.ingested_at = datetime.now(UTC)

        return IngestOutcome(
            document_id=document.id,
            status=status,
            chunks=document.chunk_count,
            embedded=document.embedded_chunk_count,
            linked_policy_id=document.policy_id,
            error=error,
        )


def _is_transient(exc: Exception) -> bool:
    """Whether retrying could plausibly succeed.

    By type, never by reading the exception's message. A substring matcher retries a
    document forever because the *document* mentions the word "timeout" — and a
    policy wording, unlike a loss notice, almost certainly does.
    """
    if isinstance(exc, AIProviderError):
        return exc.retryable
    if isinstance(exc, ExternalServiceError | httpx.TransportError | TransientIngestError):
        return True

    # `OSError` — which is what `ConnectionError`, `TimeoutError` and every socket
    # failure are — is transient. Without this line a vector-store client that raises
    # its own connection error rather than an `httpx` one is classified permanent, the
    # fingerprint is stamped, and the wording is never retried: a policy silently
    # absent from a library people are matching against. That is a worse outcome here
    # than on the claim side, where the passages are still keyword-searchable.
    if isinstance(exc, OSError):
        return True

    from sqlalchemy.exc import OperationalError

    return isinstance(exc, OperationalError)


def extraction_settled(document: PolicyDocument) -> bool:
    """Whether the text has been read, whatever the outcome was."""
    return document.extraction_status != DocumentExtractionStatus.PENDING


__all__ = [
    "IngestOutcome",
    "PolicyIngestionService",
    "TransientIngestError",
    "extraction_settled",
]
