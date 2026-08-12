"""Document ingestion.

A reusable pipeline, not an FNOL one: store the bytes, work out what the file is,
get whatever text can be got out of it, and hand that text to whoever asked. The
FNOL module is the first caller; the claim workbench will be the second.

`text.py` holds the extractors and the registry they are looked up in, which is
the seam an OCR or document-intelligence provider plugs into — a scanned PDF
comes back as `UNSUPPORTED` today with the reason attached, and becomes supported
by registering an extractor rather than by editing anything here.
"""

from __future__ import annotations

from app.services.documents.service import DocumentProcessingService, StoredDocument
from app.services.documents.text import (
    DocumentTextExtractor,
    ExtractedDocumentText,
    extract_text,
    register_extractor,
)
from app.services.documents.validation import DocumentValidationError, validate_upload

__all__ = [
    "DocumentProcessingService",
    "DocumentTextExtractor",
    "DocumentValidationError",
    "ExtractedDocumentText",
    "StoredDocument",
    "extract_text",
    "register_extractor",
    "validate_upload",
]
