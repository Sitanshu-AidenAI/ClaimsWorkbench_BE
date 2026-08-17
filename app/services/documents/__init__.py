"""Document ingestion.

A reusable pipeline, not an FNOL one: store the bytes, work out what the file is,
get whatever text can be got out of it, and hand that text to whoever asked. The
FNOL module is the first caller; the claim workbench will be the second.

`text.py` is the registry the readers are looked up in, which is the seam an OCR or
document-intelligence provider plugs into — a scanned PDF comes back as
`UNSUPPORTED` today with the reason attached, and becomes supported by registering a
reader rather than by editing anything here. The readers themselves are one module
per format under `formats/`.
"""

from __future__ import annotations

from app.services.documents.extracted import (
    ExtractedDocumentText,
    ExtractedPage,
)
from app.services.documents.service import DocumentProcessingService, StoredDocument
from app.services.documents.text import (
    DocumentTextExtractor,
    extract_text,
    register_extractor,
)
from app.services.documents.validation import DocumentValidationError, validate_upload

__all__ = [
    "DocumentProcessingService",
    "DocumentTextExtractor",
    "DocumentValidationError",
    "ExtractedDocumentText",
    "ExtractedPage",
    "StoredDocument",
    "extract_text",
    "register_extractor",
    "validate_upload",
]
