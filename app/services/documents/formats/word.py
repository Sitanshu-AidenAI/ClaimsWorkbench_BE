"""Reading a Word document.

Named `word` rather than `docx` deliberately: a module called `docx.py` that does
`import docx` is a shadowing bug waiting for the one import resolver that reads it
relatively.

`python-docx` walks the document body in order, which is the whole reason to use it
over the zip-and-parse reader this replaces. That reader collected every `<w:t>` in
the file, so a form's table cells arrived as one undifferentiated run of words with
their labels detached from their values — and a claim notification form is mostly
tables, so that came close to losing the document.

The IIF pipeline solves the same ordering problem by assigning a `position` counter
during a raw XML walk and sorting a merged stream of blocks and tables afterwards.
Iterating `document.element.body` in order gets the identical result for free, so
this reads at that level rather than porting IIF's ~800 lines of OOXML style,
numbering and merge resolution — depth that buys markdown fidelity for a chat
product and nothing for field extraction.

The previous reader is kept, as the fallback, and used all-or-nothing when this one
returns no text — IIF's rule exactly, and for its reason: a document that defeats
the structured reader may still give up its words to a blunter one.

Word has no pages until it is laid out for a printer, and this does not lay it out.
Pages are therefore **not** invented: IIF slices DOCX text into 3000-character
pseudo-pages, and a citation reading "page 3" that corresponds to nothing an officer
can see on screen is worse than a citation with no page at all. Each section carries
its nearest heading instead, which is both true and more useful to click through to.
"""

from __future__ import annotations

import io
from typing import Any

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.logging import get_logger
from app.services.documents.extracted import ExtractedDocumentText, ExtractedPage
from app.services.documents.formats.ooxml import extract_ooxml_text
from app.services.documents.formats.tables import render_table

logger = get_logger(__name__)

EXTRACTOR = "docx"
EXTRACTOR_VERSION = "1"

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPH_TAG = f"{_WORD_NS}p"
_TABLE_TAG = f"{_WORD_NS}tbl"

#: A heading style's name starts with one of these. Checked as a lowercased prefix
#: so "Heading 2" and "heading 2" both land, as does Word's "Title".
_HEADING_PREFIXES = ("heading", "title", "subtitle")
_LIST_PREFIXES = ("list", "bullet")


def extract_docx(content: bytes) -> ExtractedDocumentText:
    """Paragraphs, headings and tables, in the order the document holds them."""
    try:
        sections, table_count = _read_sections(content)
    except Exception as exc:
        logger.info("docx_structured_read_failed", error=type(exc).__name__)
        sections, table_count = [], 0

    if not sections:
        return _fallback(content)

    result = ExtractedDocumentText.from_pages(
        [
            ExtractedPage(number=index, text=text, label=label)
            for index, (label, text) in enumerate(sections, start=1)
        ],
        extractor=EXTRACTOR,
        table_count=table_count,
        # Left as None rather than guessed: see the module docstring.
        page_count=None,
    )
    if not result.text:
        return _fallback(content)
    return result


def _read_sections(content: bytes) -> tuple[list[tuple[str | None, str]], int]:
    """The document as `(heading, text)` sections, split at every heading.

    Sections rather than one blob because a chunk's section label is what stands in
    for a page number in a format that has none — "Description of loss" is a
    citation an officer can act on, and character offset 8,412 is not.
    """
    document = docx.Document(io.BytesIO(content))

    sections: list[tuple[str | None, str]] = []
    label: str | None = None
    lines: list[str] = []
    tables = 0

    def flush() -> None:
        body = "\n".join(line for line in lines if line.strip())
        if body.strip():
            sections.append((label, body))
        lines.clear()

    for element in document.element.body.iterchildren():
        if element.tag == _PARAGRAPH_TAG:
            rendered, heading = _render_paragraph(Paragraph(element, document))
            if heading is not None:
                flush()
                label = heading
            if rendered:
                lines.append(rendered)
        elif element.tag == _TABLE_TAG:
            rendered = render_table(_table_rows(Table(element, document)))
            if rendered:
                tables += 1
                lines.append(rendered)

    flush()
    return sections, tables


def _render_paragraph(paragraph: Any) -> tuple[str, str | None]:
    """`(text, heading)` — `heading` is set only when this paragraph is one."""
    text = (paragraph.text or "").strip()
    if not text:
        return "", None

    style = (getattr(paragraph.style, "name", "") or "").lower()

    if style.startswith(_HEADING_PREFIXES):
        return f"{'#' * _heading_level(style)} {text}", text
    if style.startswith(_LIST_PREFIXES):
        return f"- {text}", None
    return text, None


def _heading_level(style: str) -> int:
    digits = [character for character in style if character.isdigit()]
    if not digits:
        return 1
    return min(6, max(1, int(digits[0])))


def _table_rows(table: Any) -> list[list[str]]:
    """A table's cells as rows of text, with merged cells de-duplicated.

    `python-docx` reports a horizontally merged cell once per grid column it spans,
    so the same text arrives two or three times in one row. Collapsing adjacent
    repeats is what IIF's `_collapse_repeated_cell_runs` does after resolving
    `gridSpan`, and it reaches the same answer without reading the merge markup.
    """
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        previous = ""
        for cell in row.cells:
            text = " ".join((cell.text or "").split())
            cells.append("" if text and text == previous else text)
            previous = text or previous
        rows.append(cells)
    return rows


def _fallback(content: bytes) -> ExtractedDocumentText:
    """The blunt reader: every text node in the document part, in file order."""
    result = extract_ooxml_text(content, members=("word/document.xml",), prefix="word/")
    if result.text:
        logger.info("docx_fallback_reader_used", characters=result.characters)
        result.extractor = f"{EXTRACTOR}_fallback"
        result.warnings = (
            *result.warnings,
            "This document's structure could not be read, so its text was recovered "
            "without table layout or headings.",
        )
    return result


__all__ = ["EXTRACTOR", "EXTRACTOR_VERSION", "extract_docx"]
