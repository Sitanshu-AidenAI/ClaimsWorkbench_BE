"""Reading a PDF.

Two readers, because one cannot do both jobs. `pypdfium2` (PDFium, the engine
Chrome renders PDFs with) gives fast, accurate text **page by page**, which is what
a citation needs and what this repo's previous hand-rolled reader could not give at
all. `pdfplumber` gives table structure, which PDFium's text extraction flattens
into a stream of cells whose columns no longer mean anything.

Neither is PyMuPDF. `pymupdf4llm` is the better tool for this and it is what the
IIF pipeline uses, but PyMuPDF is AGPL-3.0-or-commercial, which for a
carrier-deployed product is a legal question rather than a technical one.
`pypdfium2` is Apache-2.0/BSD-3 and `pdfplumber` is MIT.

Two deliberate deviations from the IIF pipeline, both worth stating:

* **Sequential, not `asyncio.gather`.** IIF runs its two PDF readers concurrently
  in threads. `pdfplumber` is pure Python and holds the GIL, so overlapping it with
  PDFium's native call buys a fraction of one reader's time. The whole parse is
  moved off the event loop by the caller instead, which keeps the loop free for the
  entire duration rather than for two parts of it.
* **Tables are appended to their own page, not spliced into the paragraph flow.**
  Placing a table at its true position in the text would need a
  bbox-to-character-offset map, which PDFium does not offer. Per-page placement is
  what citations resolve against, so that is the granularity this reads at.
"""

from __future__ import annotations

import io
from typing import Any

from app.core.logging import get_logger
from app.services.documents.extracted import ExtractedDocumentText, ExtractedPage, clean_text
from app.services.documents.formats.tables import render_table

logger = get_logger(__name__)

EXTRACTOR = "pdf_text_layer"
EXTRACTOR_VERSION = "1"

#: Tables rendered per page. A page reporting more than this is a page where the
#: table detector has found structure in a layout, and the twentieth grid is noise.
_MAX_TABLES_PER_PAGE = 20

#: Cells shorter than this are not distinctive enough to test for presence in the
#: page text — "12", "£", "Y" appear everywhere.
_MIN_DISTINCTIVE_CELL = 3


def extract_pdf(content: bytes) -> ExtractedDocumentText:
    """Per-page text from the PDF's text layer, with its tables rendered.

    A PDF with no text layer is a scan. It comes back `UNSUPPORTED` with a sentence
    naming OCR as the remedy rather than as an empty success, so the officer is told
    why the document contributed nothing.
    """
    try:
        page_texts = _page_texts(content)
    except Exception as exc:
        logger.warning("pdf_text_layer_failed", error=type(exc).__name__)
        return ExtractedDocumentText.failed(
            f"This PDF could not be opened: {type(exc).__name__}.", extractor=EXTRACTOR
        )

    tables_by_page = _tables_by_page(content, page_count=len(page_texts))

    pages: list[ExtractedPage] = []
    table_count = 0
    for index, body in enumerate(page_texts, start=1):
        rendered = tables_by_page.get(index, [])
        additions = [table for table in (_useful(table, body) for table in rendered) if table]
        table_count += len(additions)
        combined = "\n\n".join([body, *additions]) if additions else body
        pages.append(ExtractedPage(number=index, text=combined))

    result = ExtractedDocumentText.from_pages(
        pages,
        extractor=EXTRACTOR,
        table_count=table_count,
        page_count=len(page_texts) or None,
    )

    if not result.text:
        return ExtractedDocumentText.unsupported(
            "This PDF has no extractable text layer — it is most likely a scan. "
            "Optical character recognition is required to read it.",
            extractor=EXTRACTOR,
            page_count=len(page_texts) or None,
        )
    return result


def page_count(content: bytes) -> int:
    """How many pages, without reading any of them. Used by the OCR detector."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(content)
    try:
        return len(document)
    finally:
        document.close()


def image_only_page_ratio(content: bytes) -> tuple[int, int]:
    """`(image_only_pages, total_pages)` — the OCR detector's third signal.

    Returns `(0, 0)` for a PDF that cannot be inspected. That is "no signal", not
    an error: the detector has two cheaper signals and a malformed page tree must
    not fail the document.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        document = pdfium.PdfDocument(content)
    except Exception:
        return 0, 0

    try:
        total = len(document)
        image_only = 0
        for index in range(total):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    has_text = bool(textpage.get_text_range().strip())
                finally:
                    textpage.close()
                has_images = any(
                    obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE for obj in page.get_objects()
                )
            finally:
                page.close()
            if has_images and not has_text:
                image_only += 1
        return image_only, total
    except Exception:
        return 0, 0
    finally:
        document.close()


def _page_texts(content: bytes) -> list[str]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(content)
    try:
        texts: list[str] = []
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    texts.append(clean_text(textpage.get_text_range()))
                finally:
                    textpage.close()
            finally:
                page.close()
        return texts
    finally:
        document.close()


def _tables_by_page(content: bytes, *, page_count: int) -> dict[int, list[str]]:
    """Rendered tables, keyed by 1-indexed page.

    Table detection failing is not the document failing. A PDF whose tables cannot
    be found still has text, and losing the text because `pdfplumber` disagreed
    with `pdfminer` about a font would be the wrong trade.
    """
    del page_count

    try:
        import pdfplumber
    except ImportError:  # pragma: no cover — the dependency is declared
        return {}

    found: dict[int, list[str]] = {}
    try:
        with pdfplumber.open(io.BytesIO(content)) as document:
            for index, page in enumerate(document.pages, start=1):
                rendered = _render_page_tables(page)
                if rendered:
                    found[index] = rendered
    except Exception as exc:
        logger.info("pdf_table_extraction_skipped", error=type(exc).__name__)
        return found

    return found


def _render_page_tables(page: Any) -> list[str]:
    try:
        tables = page.extract_tables()
    except Exception:
        return []

    rendered: list[str] = []
    for rows in tables[:_MAX_TABLES_PER_PAGE]:
        text = render_table(rows)
        if text:
            rendered.append(text)
    return rendered


def _useful(table: str, page_text: str) -> str | None:
    """Drop a rendered table the page text already says.

    PDFium reads a table's cells as flowed text, so emitting the rendered table as
    well duplicates its content. For a matrix the duplication is worth paying —
    the grid restores the column association the flowed text lost. For a
    `Label: value` form it usually is not, unless the rendering has actually
    produced pairings the flat text does not contain, which is precisely the case
    the deterministic reader downstream needs.
    """
    if not table:
        return None
    if table.startswith("|"):
        return table

    existing = {line.strip() for line in page_text.splitlines()}
    fresh = [
        line
        for line in table.splitlines()
        if line.strip() not in existing and len(line.strip()) >= _MIN_DISTINCTIVE_CELL
    ]
    return "\n".join(fresh) if fresh else None


__all__ = [
    "EXTRACTOR",
    "EXTRACTOR_VERSION",
    "extract_pdf",
    "image_only_page_ratio",
    "page_count",
]
