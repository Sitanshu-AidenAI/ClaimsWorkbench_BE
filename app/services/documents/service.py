"""The document pipeline: validate, store, read, report.

Deliberately free of FNOL vocabulary. It takes bytes and a name, and returns
what it stored and what it could read. The FNOL service is what turns that into a
`FNOLDocument` row — which is why this service can be reused by the claim
workbench without dragging intake concepts into it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.logging import get_logger
from app.domain.enums import DocumentExtractionStatus
from app.services.documents.store import DocumentStore, get_document_store, storage_key
from app.services.documents.text import ExtractedDocumentText, extract_text
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

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


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

        extracted: ExtractedDocumentText = extract_text(content_type, content)
        logger.info(
            "document_processed",
            owner=owner_reference,
            content_type=content_type,
            size_bytes=len(content),
            extraction_status=extracted.status.value,
            text_characters=extracted.characters,
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
        )

    async def fetch(self, key: str) -> bytes:
        return await self._store.get(key)

    async def remove(self, key: str) -> None:
        await self._store.delete(key)
