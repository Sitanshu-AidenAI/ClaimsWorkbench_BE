"""The dependency-free Office reader, kept as a fallback.

This is the reader that used to be the only one: unzip the container, walk the XML
with the standard library, collect every text node. It loses table layout and
heading structure, which is why it is no longer the primary reader for either
format — but it depends on nothing, and a document that defeats `python-docx` or
`openpyxl` may still give up its words to this.

Kept as a module of its own rather than left in `text.py` so the format readers can
call it as an explicit fallback rather than by importing from the registry that
dispatches to them.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from app.domain.enums import DocumentExtractionStatus
from app.services.documents.extracted import ExtractedDocumentText, clean_text

_TEXT_TAGS = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
}
_PARAGRAPH_TAGS = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row",
}

#: Parts read beyond the named ones. A document with hundreds of parts is a
#: document whose headers, footers and comments are not worth the parse.
_MAX_PARTS = 20


def extract_ooxml_text(
    content: bytes, *, members: tuple[str, ...], prefix: str | None = None
) -> ExtractedDocumentText:
    """Every text node in the named parts, plus any others under `prefix`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return ExtractedDocumentText.failed("The file is not a readable Office document.")

    names = set(archive.namelist())
    parts = [name for name in members if name in names]
    if prefix:
        parts += sorted(
            name
            for name in names
            if name.startswith(prefix) and name.endswith(".xml") and name not in parts
        )

    chunks: list[str] = []
    for part in parts[:_MAX_PARTS]:
        try:
            root = ElementTree.fromstring(archive.read(part))
        except (ElementTree.ParseError, KeyError):
            continue
        for element in root.iter():
            if element.tag in _TEXT_TAGS and element.text:
                chunks.append(element.text)
            elif element.tag in _PARAGRAPH_TAGS:
                chunks.append("\n")

    text = clean_text("".join(chunks))
    if not text:
        return ExtractedDocumentText.unsupported("No text was found in the document.")

    return ExtractedDocumentText(
        text=text,
        status=DocumentExtractionStatus.EXTRACTED,
    )


__all__ = ["extract_ooxml_text"]
