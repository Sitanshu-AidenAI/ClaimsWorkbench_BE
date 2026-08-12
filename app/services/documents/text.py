"""Getting text out of a document.

A registry of extractors keyed by content type, so support for a new format — or
for a real OCR provider — is a registration rather than a change to the pipeline.
Each extractor is a pure function from bytes to `ExtractedDocumentText`; none of
them raise, because a document that cannot be read is a normal state that the
officer is told about, not a failure of the request that uploaded it.

The PDF and Office extractors here are deliberately modest, dependency-free
readers: they recover the text layer of a digitally-produced file and say
`UNSUPPORTED` for a scan. That is an honest boundary — a scanned survey report
needs OCR, and pretending otherwise would put an empty extraction on screen with
no explanation.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from xml.etree import ElementTree

from app.core.logging import get_logger
from app.domain.enums import DocumentExtractionStatus

logger = get_logger(__name__)

#: Text kept per document. Beyond this a schedule of values is padding the prompt
#: rather than informing it.
MAX_TEXT_CHARACTERS = 40_000


@dataclass(slots=True)
class ExtractedDocumentText:
    text: str
    status: DocumentExtractionStatus
    page_count: int | None = None
    error: str | None = None

    @property
    def characters(self) -> int:
        return len(self.text)


DocumentTextExtractor = Callable[[bytes], ExtractedDocumentText]

_REGISTRY: dict[str, DocumentTextExtractor] = {}


def register_extractor(content_type: str, extractor: DocumentTextExtractor) -> None:
    """Register (or replace) the extractor for a content type."""
    _REGISTRY[content_type] = extractor


def extract_text(content_type: str, content: bytes) -> ExtractedDocumentText:
    """Read a document, never raising. An unknown type is `UNSUPPORTED`."""
    extractor = _REGISTRY.get(content_type)
    if extractor is None:
        if content_type.startswith("text/"):
            extractor = _plain_text
        else:
            return ExtractedDocumentText(
                text="",
                status=DocumentExtractionStatus.UNSUPPORTED,
                error=f"No text extractor is registered for {content_type}.",
            )

    try:
        return extractor(content)
    except Exception as exc:
        logger.warning("document_text_extraction_failed", content_type=content_type)
        return ExtractedDocumentText(
            text="",
            status=DocumentExtractionStatus.FAILED,
            error=f"The file could not be read: {type(exc).__name__}.",
        )


def _clean(text: str) -> str:
    collapsed = re.sub(r"[ \t\x0b\f]+", " ", text)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip()[:MAX_TEXT_CHARACTERS]


def _plain_text(content: bytes) -> ExtractedDocumentText:
    text = content.decode("utf-8", errors="replace")
    return ExtractedDocumentText(text=_clean(text), status=DocumentExtractionStatus.EXTRACTED)


def _json_text(content: bytes) -> ExtractedDocumentText:
    """A structured payload, flattened to `key: value` lines.

    Flattened rather than pretty-printed: the extractor downstream reads labelled
    lines, and a JSON tree rendered as labelled lines is exactly that.
    """
    try:
        payload = json.loads(content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return ExtractedDocumentText(
            text="", status=DocumentExtractionStatus.FAILED, error=f"Invalid JSON: {exc.msg}."
        )

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
    return ExtractedDocumentText(
        text=_clean("\n".join(lines)), status=DocumentExtractionStatus.EXTRACTED
    )


def _csv_text(content: bytes) -> ExtractedDocumentText:
    """A spreadsheet export, rendered row by row with its headers repeated.

    Repeating the header per row costs characters and buys the one thing that
    matters: a row is readable on its own, so "Estimated cost: 42,000" survives
    being read out of context.
    """
    decoded = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(decoded))
    rows = list(reader)
    if not rows:
        return ExtractedDocumentText(text="", status=DocumentExtractionStatus.EXTRACTED)

    header, *body = rows
    if not body:
        return ExtractedDocumentText(
            text=_clean(", ".join(header)), status=DocumentExtractionStatus.EXTRACTED
        )

    lines = [
        "; ".join(
            f"{column.strip()}: {value.strip()}"
            for column, value in zip(header, row, strict=False)
            if value.strip()
        )
        for row in body[:500]
    ]
    return ExtractedDocumentText(
        text=_clean("\n".join(line for line in lines if line)),
        status=DocumentExtractionStatus.EXTRACTED,
    )


_PDF_TEXT_OP_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def _pdf_text(content: bytes) -> ExtractedDocumentText:
    """The text layer of a digitally-produced PDF.

    Walks the content streams, inflating the Flate-compressed ones, and collects
    the string operands of the text-showing operators. It does not attempt font
    re-encoding or layout reconstruction — a PDF whose text layer is a scan comes
    back empty, and is reported as needing OCR rather than as read.
    """
    pages = content.count(b"/Type/Page") + content.count(b"/Type /Page")
    chunks: list[str] = []

    for raw in _PDF_STREAM_RE.findall(content):
        stream = raw
        # An uncompressed or unsupported-filter stream is tried as it stands.
        with contextlib.suppress(zlib.error):
            stream = zlib.decompress(raw)

        if b"TJ" not in stream and b"Tj" not in stream:
            continue

        for token in _PDF_TEXT_OP_RE.findall(stream):
            chunks.append(_decode_pdf_string(token))

    text = _clean(" ".join(chunk for chunk in chunks if chunk.strip()))
    if not text:
        return ExtractedDocumentText(
            text="",
            status=DocumentExtractionStatus.UNSUPPORTED,
            page_count=pages or None,
            error=(
                "This PDF has no extractable text layer — it is most likely a scan. "
                "Optical character recognition is required to read it."
            ),
        )
    return ExtractedDocumentText(
        text=text, status=DocumentExtractionStatus.EXTRACTED, page_count=pages or None
    )


_PDF_ESCAPES = {
    b"n": "\n",
    b"r": "\r",
    b"t": "\t",
    b"b": "\b",
    b"f": "\f",
    b"(": "(",
    b")": ")",
    b"\\": "\\",
}


def _decode_pdf_string(token: bytes) -> str:
    if token.startswith(b"<"):
        hex_digits = re.sub(rb"[^0-9A-Fa-f]", b"", token)
        if len(hex_digits) % 2:
            hex_digits += b"0"
        try:
            decoded = bytes.fromhex(hex_digits.decode("ascii"))
        except ValueError:
            return ""
        # UTF-16BE with a byte-order mark is what most producers emit for hex.
        if decoded.startswith(b"\xfe\xff"):
            return decoded[2:].decode("utf-16-be", errors="replace")
        return decoded.decode("latin-1", errors="replace")

    body = token[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index : index + 1]
        if char == b"\\" and index + 1 < len(body):
            following = body[index + 1 : index + 2]
            out.append(_PDF_ESCAPES.get(following, following.decode("latin-1", errors="replace")))
            index += 2
            continue
        out.append(char.decode("latin-1", errors="replace"))
        index += 1
    return "".join(out)


_OOXML_TEXT_TAGS = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
}
_OOXML_PARAGRAPH_TAGS = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row",
}


def _ooxml_text(members: tuple[str, ...]) -> DocumentTextExtractor:
    """Build an extractor for an Office Open XML container.

    `.docx` and `.xlsx` are both zip archives of XML; the only difference that
    matters here is which members hold the text.
    """

    def extractor(content: bytes) -> ExtractedDocumentText:
        import zipfile

        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            return ExtractedDocumentText(
                text="",
                status=DocumentExtractionStatus.FAILED,
                error="The file is not a readable Office document.",
            )

        names = set(archive.namelist())
        parts = [name for name in members if name in names]
        parts += [
            name
            for name in names
            if name.startswith("word/") and name.endswith(".xml") and name not in parts
        ]

        chunks: list[str] = []
        for part in parts[:20]:
            try:
                root = ElementTree.fromstring(archive.read(part))
            except (ElementTree.ParseError, KeyError):
                continue
            for element in root.iter():
                if element.tag in _OOXML_TEXT_TAGS and element.text:
                    chunks.append(element.text)
                elif element.tag in _OOXML_PARAGRAPH_TAGS:
                    chunks.append("\n")

        text = _clean("".join(chunks))
        if not text:
            return ExtractedDocumentText(
                text="",
                status=DocumentExtractionStatus.UNSUPPORTED,
                error="No text was found in the document.",
            )
        return ExtractedDocumentText(text=text, status=DocumentExtractionStatus.EXTRACTED)

    return extractor


def _binary_office(content: bytes) -> ExtractedDocumentText:
    """Legacy `.doc` / `.xls`.

    The pre-2007 compound-document formats need a dedicated parser, which is a
    dependency this service does not carry. Reported rather than half-read.
    """
    del content
    return ExtractedDocumentText(
        text="",
        status=DocumentExtractionStatus.UNSUPPORTED,
        error=(
            "Legacy Word and Excel formats are not read directly. "
            "Re-save as .docx/.xlsx or PDF to have the text extracted."
        ),
    )


def _image(content: bytes) -> ExtractedDocumentText:
    """A photograph or scan.

    Held with its metadata and shown to the officer; no text is claimed from it.
    Registering an OCR extractor for `image/*` is what turns this on.
    """
    del content
    return ExtractedDocumentText(
        text="",
        status=DocumentExtractionStatus.UNSUPPORTED,
        error="Images are stored as evidence; optical character recognition is not enabled.",
    )


for _content_type in ("text/plain", "text/markdown", "message/rfc822"):
    register_extractor(_content_type, _plain_text)
register_extractor("application/json", _json_text)
register_extractor("text/csv", _csv_text)
register_extractor("application/pdf", _pdf_text)
register_extractor(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    _ooxml_text(("word/document.xml",)),
)
register_extractor(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    _ooxml_text(("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml")),
)
register_extractor("application/msword", _binary_office)
register_extractor("application/vnd.ms-excel", _binary_office)
for _content_type in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/tiff"):
    register_extractor(_content_type, _image)
