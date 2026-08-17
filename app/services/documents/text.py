"""Getting text out of a document.

A registry of readers keyed by content type, so support for a new format — or for a
real OCR provider — is a registration rather than a change to the pipeline. Each
reader is a function from bytes to `ExtractedDocumentText`; none of them raise,
because a document that cannot be read is a normal state that the officer is told
about, not a failure of the request that uploaded it.

The readers themselves live in `app/services/documents/formats/`. This module is the
dispatch and nothing else: the three simple ones below (plain text, JSON, CSV) stay
here because each is a dozen lines and moving them would buy a file, not a boundary.

What each format is read with, and why:

| Format          | Reader                              |
|-----------------|-------------------------------------|
| PDF             | `pypdfium2` text layer + `pdfplumber` tables |
| `.docx`         | `python-docx` in body order, stdlib zip reader as fallback |
| `.xlsx`         | `openpyxl`, values not formulas, one page per sheet |
| `.eml`          | stdlib `email`, HTML bodies through `strip_html` |
| `.msg`          | `olefile` over the MAPI property streams |
| images, scans   | nothing yet — `UNSUPPORTED`, pending OCR |
| `.doc`, `.xls`  | nothing — the pre-2007 binary formats |
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable

from app.core.logging import get_logger
from app.services.documents.extracted import (
    MAX_TEXT_CHARACTERS,
    ExtractedDocumentText,
    ExtractedPage,
    clean_text,
)
from app.services.documents.formats import (
    extract_docx,
    extract_eml,
    extract_msg,
    extract_pdf,
    extract_xlsx,
)

logger = get_logger(__name__)

#: Rows of a CSV rendered. Beyond this the file is a data export rather than a
#: claims document, and row 501 will not change what the notice says.
MAX_CSV_ROWS = 500

type DocumentTextExtractor = Callable[[bytes], ExtractedDocumentText]

_REGISTRY: dict[str, DocumentTextExtractor] = {}


def register_extractor(content_type: str, extractor: DocumentTextExtractor) -> None:
    """Register (or replace) the reader for a content type.

    Replacing is supported on purpose: it is how a deployment swaps in an OCR-backed
    PDF reader, and how a test substitutes a reader it can control.
    """
    _REGISTRY[content_type] = extractor


def registered_extractor(content_type: str) -> DocumentTextExtractor | None:
    """The reader for a content type, or `None`. Exposed for tests and diagnostics."""
    return _REGISTRY.get(content_type)


def extract_text(content_type: str, content: bytes) -> ExtractedDocumentText:
    """Read a document, never raising. An unknown type is `UNSUPPORTED`."""
    extractor = _REGISTRY.get(content_type)
    if extractor is None:
        if content_type.startswith("text/"):
            extractor = _plain_text
        else:
            return ExtractedDocumentText.unsupported(
                f"No text extractor is registered for {content_type}."
            )

    try:
        return extractor(content)
    except Exception as exc:
        # The reader was supposed to handle this itself. Catching here anyway is
        # what keeps a malformed attachment from failing the upload that carried it.
        logger.warning(
            "document_text_extraction_failed",
            content_type=content_type,
            error=type(exc).__name__,
        )
        return ExtractedDocumentText.failed(f"The file could not be read: {type(exc).__name__}.")


# --- the simple readers -------------------------------------------------------


def _plain_text(content: bytes) -> ExtractedDocumentText:
    text = clean_text(content.decode("utf-8", errors="replace"))
    if not text:
        return ExtractedDocumentText.unsupported("The file contained no text.", extractor="plain")
    return ExtractedDocumentText.from_pages(
        [ExtractedPage(number=1, text=text)], extractor="plain", page_count=None
    )


def _json_text(content: bytes) -> ExtractedDocumentText:
    """A structured payload, flattened to `key: value` lines.

    Flattened rather than pretty-printed: the extractor downstream reads labelled
    lines, and a JSON tree rendered as labelled lines is exactly that.
    """
    try:
        payload = json.loads(content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return ExtractedDocumentText.failed(f"Invalid JSON: {exc.msg}.", extractor="json")

    lines: list[str] = []

    def walk(node: object, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}{key}." if prefix else f"{key}.")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{prefix[:-1]}[{index}].")
        else:
            lines.append(f"{prefix.rstrip('.').replace('.', ' ')}: {node}")

    walk(payload)
    text = clean_text("\n".join(lines))
    if not text:
        return ExtractedDocumentText.unsupported("The payload was empty.", extractor="json")
    return ExtractedDocumentText.from_pages(
        [ExtractedPage(number=1, text=text)], extractor="json", page_count=None
    )


def _csv_text(content: bytes) -> ExtractedDocumentText:
    """A spreadsheet export, rendered row by row with its headers repeated.

    Repeating the header per row costs characters and buys the one thing that
    matters: a row is readable on its own, so "Estimated cost: 42,000" survives
    being read out of context.
    """
    decoded = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(decoded)))
    if not rows:
        return ExtractedDocumentText.unsupported("The file was empty.", extractor="csv")

    header, *body = rows
    if not body:
        text = clean_text(", ".join(header))
    else:
        lines = [
            "; ".join(
                f"{column.strip()}: {value.strip()}"
                for column, value in zip(header, row, strict=False)
                if value.strip()
            )
            for row in body[:MAX_CSV_ROWS]
        ]
        text = clean_text("\n".join(line for line in lines if line))

    if not text:
        return ExtractedDocumentText.unsupported("The file held no values.", extractor="csv")

    warnings = (
        (f"Only the first {MAX_CSV_ROWS:,} rows were read.",) if len(body) > MAX_CSV_ROWS else ()
    )
    return ExtractedDocumentText.from_pages(
        [ExtractedPage(number=1, text=text)],
        extractor="csv",
        page_count=None,
        warnings=warnings,
    )


def _binary_office(content: bytes) -> ExtractedDocumentText:
    """Legacy `.doc` / `.xls`.

    The pre-2007 compound-document formats need a dedicated record parser, which is
    a dependency this service does not carry. Reported rather than half-read.
    """
    del content
    return ExtractedDocumentText.unsupported(
        "Legacy Word and Excel formats are not read directly. "
        "Re-save as .docx/.xlsx or PDF to have the text extracted.",
        extractor="binary_office",
    )


def _image(content: bytes) -> ExtractedDocumentText:
    """A photograph or scan.

    Held with its metadata and shown to the officer; no text is claimed from it.
    Registering an OCR reader for `image/*` is what turns this on.
    """
    del content
    return ExtractedDocumentText.unsupported(
        "Images are stored as evidence; optical character recognition is not enabled.",
        extractor="image",
    )


# --- registrations ------------------------------------------------------------

for _content_type in ("text/plain", "text/markdown"):
    register_extractor(_content_type, _plain_text)
register_extractor("application/json", _json_text)
register_extractor("text/csv", _csv_text)

register_extractor("application/pdf", extract_pdf)
register_extractor(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    extract_docx,
)
register_extractor(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    extract_xlsx,
)
register_extractor("message/rfc822", extract_eml)
register_extractor("application/vnd.ms-outlook", extract_msg)

register_extractor("application/msword", _binary_office)
register_extractor("application/vnd.ms-excel", _binary_office)
for _content_type in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/tiff"):
    register_extractor(_content_type, _image)


__all__ = [
    "MAX_TEXT_CHARACTERS",
    "ExtractedDocumentText",
    "ExtractedPage",
    "extract_text",
    "register_extractor",
    "registered_extractor",
]
