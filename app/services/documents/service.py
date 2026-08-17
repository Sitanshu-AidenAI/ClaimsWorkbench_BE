"""The document pipeline: validate, store, read, report.

Deliberately free of FNOL vocabulary. It takes bytes and a name, and returns
what it stored and what it could read. The FNOL service is what turns that into a
`FNOLDocument` row — which is why this service can be reused by the claim
workbench without dragging intake concepts into it.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.domain.enums import DocumentExtractionStatus
from app.services.documents.extracted import ExtractedDocumentText, ExtractedPage
from app.services.documents.store import DocumentStore, get_document_store, storage_key
from app.services.documents.text import extract_text
from app.services.documents.validation import document_kind, validate_upload

logger = get_logger(__name__)


@dataclass(slots=True)
class StoredDocument:
    """The outcome of processing one attachment."""

    filename: str
    content_type: str
    size_bytes: int
    checksum: str
    storage_key: str
    kind: str
    text: str
    extraction_status: DocumentExtractionStatus
    page_count: int | None
    extraction_error: str | None

    #: Which reader produced the text, and where each page landed inside it. Both
    #: are what a per-page citation is built from later; neither is derivable after
    #: the fact, because the same content type can be read more than one way.
    extractor: str | None = None
    pages: tuple[ExtractedPage, ...] = ()
    table_count: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def page_offsets(self) -> list[list[int]]:
        return [[page.char_start, page.char_end] for page in self.pages]


class DocumentProcessingService:
    def __init__(self, store: DocumentStore | None = None) -> None:
        self._store = store or get_document_store()

    async def process(
        self,
        *,
        owner_reference: str,
        filename: str,
        content: bytes,
        max_bytes: int,
        declared_content_type: str | None = None,
    ) -> StoredDocument:
        """Validate, store and read one attachment.

        Text extraction is done once, here, and the result is persisted with the
        document. Nothing re-reads a stored file to answer a later question — that
        is the difference between a page load costing a database query and costing
        a download plus a parse.
        """
        safe_name, content_type = validate_upload(
            filename, content, max_bytes=max_bytes, declared_content_type=declared_content_type
        )
        checksum = hashlib.sha256(content).hexdigest()
        key = storage_key(owner_reference, checksum, safe_name)

        await self._store.put(key, content, content_type=content_type)

        # Off the event loop. Reading a 25MB PDF is seconds of CPU-bound parsing,
        # and this method is called from inside an HTTP request — leaving it inline
        # blocks every other request on the worker for the duration. The readers stay
        # synchronous, so the thread hop belongs here rather than in each of them.
        extracted: ExtractedDocumentText = await asyncio.to_thread(
            extract_text, content_type, content
        )

        logger.info(
            "document_processed",
            owner=owner_reference,
            content_type=content_type,
            size_bytes=len(content),
            extractor=extracted.extractor,
            extraction_status=extracted.status.value,
            text_characters=extracted.characters,
            page_count=extracted.page_count,
            table_count=extracted.table_count,
        )

        return StoredDocument(
            filename=safe_name,
            content_type=content_type,
            size_bytes=len(content),
            checksum=checksum,
            storage_key=key,
            kind=document_kind(content_type),
            text=extracted.text,
            extraction_status=extracted.status,
            page_count=extracted.page_count,
            extraction_error=extracted.error,
            extractor=extracted.extractor,
            pages=extracted.pages,
            table_count=extracted.table_count,
            warnings=extracted.warnings,
        )

    async def store(self, key: str, content: bytes, *, content_type: str) -> None:
        """Write bytes this service produced rather than received.

        Deliberately not `process`: that path validates an *upload*, and its
        checks — an allowed extension, magic bytes matching the name — are about
        distrusting a client. Content the application generated, such as a
        notification body written out as a document so it can be cited, has no
        client to distrust and no extension to disagree with. Routing it through
        the upload validator would mean weakening the validator.
        """
        await self._store.put(key, content, content_type=content_type)

    async def fetch(self, key: str) -> bytes:
        return await self._store.get(key)

    async def remove(self, key: str) -> None:
        await self._store.delete(key)
