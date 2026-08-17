"""The notification body, as a document.

Before this existed, the body of the email that raised a notice was a `Text`
column and nothing else. It was pasted whole into every extraction prompt, so
values *were* read from it — and none of them could ever be cited, because a
citation resolves to a passage and the body had none. The review screen's honest
answer for a value read out of the broker's own email was "no source recorded".

Materialising the body as a `FNOLDocument` fixes that at the root rather than by
special-casing it downstream. It is stored, read, chunked, embedded, retrieved,
cited and highlighted by exactly the code that does all of those things for an
attachment, and every one of those modules stays unaware that this document is
different.

Two properties are worth stating because the rest of the module depends on them:

* **It is idempotent.** The body's text is hashed; an unchanged body produces the
  same checksum, matches the existing row, and costs one query. A corrected or
  re-collected body replaces the stored object and moves the checksum, which
  moves the index fingerprint, which re-chunks and re-extracts — exactly the
  chain an edited attachment goes through.
* **It never fails a caller.** A storage backend that is down means no body
  document this run. That is a degraded extraction, not a failed notice, and the
  next run picks it up.
"""

from __future__ import annotations

import hashlib

from app.core.logging import get_logger
from app.domain.enums import DocumentExtractionStatus, DocumentSource
from app.models.fnol import FNOLCase, FNOLDocument
from app.repositories.fnol import FNOLRepository
from app.services.documents.service import DocumentProcessingService
from app.services.documents.store import storage_key

logger = get_logger(__name__)

#: `FNOLDocument.source` for the body. Distinct from `email_attachment` and
#: `upload` so the case file can label it, the engine can always include it, and
#: a reviewer can tell "the broker wrote this" from "the broker attached this".
BODY_DOCUMENT_SOURCE = DocumentSource.NOTIFICATION_BODY

#: The filename shown in the case file. Not derived from the subject: a filename
#: that changes when somebody edits a subject line would look like a new document
#: every time.
BODY_DOCUMENT_FILENAME = "notification-body.txt"

BODY_CONTENT_TYPE = "text/plain"


class NotificationBodyDocumentService:
    """Keeps a case's notification body present as a document."""

    def __init__(self, repository: FNOLRepository, documents: DocumentProcessingService) -> None:
        self._repository = repository
        self._documents = documents

    async def ensure(self, case: FNOLCase) -> FNOLDocument | None:
        """Create or refresh the body document. Returns it, or `None`.

        `None` means there was nothing to store — a case ingested through a
        channel that carries no body — or that storing it failed. Neither is an
        error the caller should act on.
        """
        text = (case.source_body or "").strip()
        if not text:
            return None

        content = text.encode("utf-8")
        checksum = hashlib.sha256(content).hexdigest()

        existing = await self._find(case)
        if existing is not None and existing.checksum_sha256 == checksum:
            return existing

        key = storage_key(case.reference, checksum, BODY_DOCUMENT_FILENAME)
        try:
            await self._documents.store(key, content, content_type=BODY_CONTENT_TYPE)
        except Exception as exc:
            logger.warning(
                "fnol_body_document_store_failed",
                reference=case.reference,
                error=type(exc).__name__,
            )
            return None

        document = existing or self._repository.add_document(
            FNOLDocument(fnol_case_id=case.id, filename=BODY_DOCUMENT_FILENAME)
        )
        _apply(document, content=content, checksum=checksum, key=key, text=text)
        await self._repository.flush()

        logger.info(
            "fnol_body_document_written",
            reference=case.reference,
            characters=len(text),
            replaced=existing is not None,
        )
        return document

    async def _find(self, case: FNOLCase) -> FNOLDocument | None:
        """This case's body document, if it has one.

        Matched on `source` rather than on filename: the filename is display
        text, and a case must never end up with two body documents because one
        of them was named differently by an older release.
        """
        for document in await self._repository.list_documents(case.id):
            if document.source == BODY_DOCUMENT_SOURCE:
                return document
        return None


def _apply(document: FNOLDocument, *, content: bytes, checksum: str, key: str, text: str) -> None:
    """Write the body's identity and text onto the row.

    `page_offsets` covers the whole text as a single page. That is what makes the
    chunker treat the body as one region it may not cut across a boundary of, and
    what lets the evidence endpoint resolve an offset to a page at all — a body
    with no page spans would be citable but not locatable.
    """
    document.filename = BODY_DOCUMENT_FILENAME
    document.content_type = BODY_CONTENT_TYPE
    document.size_bytes = len(content)
    document.storage_key = key
    document.checksum_sha256 = checksum
    document.source = BODY_DOCUMENT_SOURCE
    document.document_kind = "notification"
    document.extraction_status = DocumentExtractionStatus.EXTRACTED
    document.extracted_text = text
    document.text_characters = len(text)
    document.page_count = 1
    document.extraction_error = None
    document.text_extractor = "notification_body"
    document.text_extractor_version = "1"
    document.page_offsets = [[0, len(text)]]
    # Cleared so the indexer treats a replaced body as new work. The signature is
    # recomputed from the checksum and the extractor by the index service; wiping
    # it here is what stops a changed body from reusing the old passages.
    document.extraction_signature = None
    document.index_fingerprint = None
    document.index_attempts = 0


__all__ = [
    "BODY_CONTENT_TYPE",
    "BODY_DOCUMENT_FILENAME",
    "BODY_DOCUMENT_SOURCE",
    "NotificationBodyDocumentService",
]
