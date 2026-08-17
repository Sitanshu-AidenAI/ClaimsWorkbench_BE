"""Reading a spreadsheet.

Three decisions carried over from the IIF pipeline, each of which changes what the
extraction downstream can see:

* **`data_only=True`.** A cell holding `=SUM(B2:B9)` reads as `42000`, not as its
  formula. A schedule of values whose totals arrive as formula strings has no totals
  in it at all.
* **Each worksheet is one page.** A sheet is a genuinely navigable unit, so unlike
  Word this format *does* get page numbers — and the sheet name travels as the
  page's label, so a citation reads "Schedule of values" rather than "page 2".
* **Contiguous non-empty row bands are treated as tables**, tolerating up to two
  blank rows inside one band. Real claims spreadsheets stack several tables on one
  sheet with a blank row between them, and reading the sheet as one grid would
  merge a summary block into the line items below it under the wrong headers.

Not ported from IIF: its Excel `ListObject` pass, its chart-derived table pass, and
its separate `data_only=False` re-read to recover formula text. The first two matter
for financial-statement analysis; the third is for showing a user the formula. None
of them help read a loss amount out of a repair estimate.

IIF switches to `read_only=True` above 10MB via a second, duplicated code path. The
same switch is here as one argument, because `iter_rows(values_only=True)` behaves
identically in both modes — the duplication was avoidable.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

from app.core.logging import get_logger
from app.services.documents.extracted import ExtractedDocumentText, ExtractedPage
from app.services.documents.formats.ooxml import extract_ooxml_text
from app.services.documents.formats.tables import render_table

logger = get_logger(__name__)

EXTRACTOR = "xlsx"
EXTRACTOR_VERSION = "1"

#: Above this, openpyxl reads row by row instead of holding the workbook in memory.
_STREAMING_THRESHOLD_BYTES = 10 * 1024 * 1024

#: Rows read per sheet. A sheet with more than this is a data export, and the
#: five-thousandth row will not tell an officer anything the first hundred did not.
_MAX_ROWS_PER_SHEET = 5_000

#: Blank rows tolerated inside one table band before it is treated as two tables.
#: IIF's value, and it matches how people actually lay a spreadsheet out.
_BLANK_ROW_TOLERANCE = 2


def extract_xlsx(content: bytes) -> ExtractedDocumentText:
    """One page per visible worksheet, each rendered as its tables."""
    try:
        sheets, table_count, truncated = _read_sheets(content)
    except Exception as exc:
        logger.info("xlsx_structured_read_failed", error=type(exc).__name__)
        return _fallback(content)

    if not sheets:
        return _fallback(content)

    warnings = (
        (f"Only the first {_MAX_ROWS_PER_SHEET:,} rows of each sheet were read.",)
        if truncated
        else ()
    )

    result = ExtractedDocumentText.from_pages(
        [
            ExtractedPage(number=index, text=text, label=name)
            for index, (name, text) in enumerate(sheets, start=1)
        ],
        extractor=EXTRACTOR,
        table_count=table_count,
        # A sheet is a real navigable unit, so unlike Word this format does have
        # pages, and a citation can name one.
        page_count=len(sheets),
        warnings=warnings,
    )
    if not result.text:
        return _fallback(content)
    return result


def _read_sheets(content: bytes) -> tuple[list[tuple[str, str]], int, bool]:
    """`([(sheet_name, text)], table_count, was_truncated)`."""
    import openpyxl

    workbook = openpyxl.load_workbook(
        io.BytesIO(content),
        data_only=True,
        read_only=len(content) >= _STREAMING_THRESHOLD_BYTES,
    )
    try:
        sheets: list[tuple[str, str]] = []
        tables = 0
        truncated = False

        for worksheet in workbook.worksheets:
            # A hidden sheet is hidden because its author did not want it read.
            if getattr(worksheet, "sheet_state", "visible") != "visible":
                continue

            rows, sheet_truncated = _sheet_rows(worksheet)
            truncated = truncated or sheet_truncated
            if not rows:
                continue

            rendered = [render_table(band) for band in _bands(rows)]
            bodies = [text for text in rendered if text]
            if not bodies:
                continue

            tables += len(bodies)
            sheets.append((worksheet.title, "\n\n".join([f"## Sheet: {worksheet.title}", *bodies])))

        return sheets, tables, truncated
    finally:
        workbook.close()


def _sheet_rows(worksheet: Any) -> tuple[list[list[str]], bool]:
    """Every row as strings, capped. `(rows, was_truncated)`."""
    rows: list[list[str]] = []
    truncated = False

    for index, values in enumerate(worksheet.iter_rows(values_only=True)):
        if index >= _MAX_ROWS_PER_SHEET:
            truncated = True
            break
        rows.append([_cell(value) for value in values])

    return rows, truncated


def _bands(rows: list[list[str]]) -> list[list[list[str]]]:
    """Split rows into table bands at runs of blank rows.

    A run of up to `_BLANK_ROW_TOLERANCE` blank rows is treated as spacing inside
    one table; a longer run separates two tables. Column stripping is left to
    `render_table`, which does it per band — stripping across the whole sheet would
    delete a column that is empty in the summary block and populated in the line
    items below it.
    """
    bands: list[list[list[str]]] = []
    current: list[list[str]] = []
    blanks = 0

    for row in rows:
        if any(cell.strip() for cell in row):
            if blanks > _BLANK_ROW_TOLERANCE and current:
                bands.append(current)
                current = []
            blanks = 0
            current.append(row)
            continue

        blanks += 1
        if current and blanks <= _BLANK_ROW_TOLERANCE:
            # Kept, so a deliberate spacer row inside a table does not split it.
            current.append(row)

    if current:
        bands.append(current)
    return bands


def _cell(value: object) -> str:
    """A cell as the text a person would read.

    Dates and whole-number floats are the two that matter. `str()` on a
    `datetime` gives `2026-03-14 00:00:00`, and on a currency cell gives
    `42000.0` — both are values a reader has to un-mangle before they can use them,
    and a date of loss is one of the fields this pipeline exists to extract.
    """
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time.min else value.isoformat(" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value).strip()


def _fallback(content: bytes) -> ExtractedDocumentText:
    """The blunt reader: the shared string table and the first worksheet's XML."""
    result = extract_ooxml_text(
        content, members=("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml")
    )
    if result.text:
        logger.info("xlsx_fallback_reader_used", characters=result.characters)
        result.extractor = f"{EXTRACTOR}_fallback"
        result.warnings = (
            *result.warnings,
            "This workbook's structure could not be read, so its text was recovered "
            "without sheet names or table layout.",
        )
    return result


__all__ = ["EXTRACTOR", "EXTRACTOR_VERSION", "extract_xlsx"]
