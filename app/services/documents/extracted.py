"""What a text extractor returns.

Split out of `text.py` so the format readers in `formats/` and the registry that
dispatches to them share one result shape without importing each other.

The shape carries **pages**, not just text, and that is the point of this module.
A claims officer reading "the policy number came from the survey report" needs the
page, and a page number can only come from an extractor that produced text page by
page. Every reader here therefore builds a list of pages and lets
`ExtractedDocumentText.from_pages` do the joining, so the character offsets that
locate a page inside the joined text are recorded by the code that does the join
rather than recomputed later by code that has to guess the separator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.enums import DocumentExtractionStatus

#: Text kept per document. Beyond this a schedule of values is padding the prompt
#: rather than informing it.
#:
#: Raising this is a separate change with a prerequisite: `FNOLCase.documents` is
#: `lazy="selectin"` and the review screen refreshes the case on every read, so a
#: larger cap multiplies the cost of a page load until `extracted_text` is
#: deferred on the model.
MAX_TEXT_CHARACTERS = 40_000

#: What goes between two pages in the joined text. Two newlines rather than a
#: `--- Page N ---` marker: the marker would be indexed, embedded and eventually
#: quoted back as evidence, and "--- Page 4 ---" is not something a document said.
PAGE_SEPARATOR = "\n\n"


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """One page of a document, and where it sits in the joined text.

    `label` is whatever the format offers as a human-readable name for the page —
    a worksheet name, the nearest heading, the subject of a forwarded message. It
    is what a citation falls back to when `number` means nothing, which is the
    normal case for every format except PDF and spreadsheets.
    """

    number: int
    text: str
    label: str | None = None
    char_start: int = 0
    char_end: int = 0


@dataclass(slots=True)
class ExtractedDocumentText:
    text: str
    status: DocumentExtractionStatus
    page_count: int | None = None
    error: str | None = None

    #: Empty for formats with no meaningful pagination, and for the failure and
    #: unsupported paths. Never partially populated: either the extractor produced
    #: pages or it did not.
    pages: tuple[ExtractedPage, ...] = ()
    #: Which reader produced this, recorded so a re-read can be skipped when the
    #: reader has not changed. Not the content type: `application/pdf` can be read
    #: from its text layer or by OCR, and those are different answers.
    extractor: str | None = None
    table_count: int = 0

    ocr_applied: bool = False
    ocr_confidence: float | None = None
    #: Why OCR ran, or why it did not — the detector's own sentence, kept so the
    #: decision is auditable without re-running it.
    ocr_reason: str | None = None

    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def characters(self) -> int:
        return len(self.text)

    @property
    def page_offsets(self) -> list[list[int]]:
        """`[[start, end], …]` per page, for storage alongside the document."""
        return [[page.char_start, page.char_end] for page in self.pages]

    @classmethod
    def from_pages(
        cls,
        pages: list[ExtractedPage],
        *,
        extractor: str,
        table_count: int = 0,
        #: How many pages the *document* has, which is not `len(pages)` — a page
        #: with no text is still a page. Left `None` for the formats that genuinely
        #: have no pagination, so it is never derived: deriving it would turn "Word
        #: has no pages" into the false claim that it has as many as it has headings.
        page_count: int | None = None,
        budget: int = MAX_TEXT_CHARACTERS,
        warnings: tuple[str, ...] = (),
    ) -> ExtractedDocumentText:
        """Join pages into one text, recording where each landed.

        Truncation drops whole pages rather than cutting one in half. A page
        sliced mid-sentence would still be offered as a citation, and the officer
        would click through to a fragment — so the honest answer is a warning
        saying the document was too long, and a page list that stops.
        """
        kept: list[ExtractedPage] = []
        parts: list[str] = []
        cursor = 0
        truncated_at: int | None = None

        for page in pages:
            body = clean_text(page.text, budget=budget)
            if not body:
                continue

            start = cursor + (len(PAGE_SEPARATOR) if parts else 0)
            end = start + len(body)
            if end > budget:
                truncated_at = page.number
                break

            if parts:
                parts.append(PAGE_SEPARATOR)
            parts.append(body)
            kept.append(
                ExtractedPage(
                    number=page.number,
                    text=body,
                    label=page.label,
                    char_start=start,
                    char_end=end,
                )
            )
            cursor = end

        notes = list(warnings)
        if truncated_at is not None:
            notes.append(
                f"The document was truncated at page {truncated_at}: only the first "
                f"{budget:,} characters are read."
            )

        text = "".join(parts)
        if not text:
            return cls(
                text="",
                status=DocumentExtractionStatus.UNSUPPORTED,
                page_count=page_count,
                extractor=extractor,
                error="No text was found in the document.",
                warnings=tuple(notes),
            )

        return cls(
            text=text,
            status=DocumentExtractionStatus.EXTRACTED,
            page_count=page_count,
            pages=tuple(kept),
            extractor=extractor,
            table_count=table_count,
            warnings=tuple(notes),
        )

    @classmethod
    def unsupported(
        cls, message: str, *, extractor: str | None = None, **kwargs: object
    ) -> ExtractedDocumentText:
        """A file this service will not claim to have read."""
        return cls(
            text="",
            status=DocumentExtractionStatus.UNSUPPORTED,
            error=message,
            extractor=extractor,
            **kwargs,  # type: ignore[arg-type]
        )

    @classmethod
    def failed(cls, message: str, *, extractor: str | None = None) -> ExtractedDocumentText:
        """A file that should have been readable and was not."""
        return cls(
            text="",
            status=DocumentExtractionStatus.FAILED,
            error=message,
            extractor=extractor,
        )


_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t\x0b\f]+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
#: Control characters that survive a bad decode. Postgres rejects NUL in a text
#: column outright, so this is a correctness fix and not tidying.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


def clean_text(text: str, *, budget: int = MAX_TEXT_CHARACTERS) -> str:
    """Collapse the whitespace a parser leaves behind, and bound the result.

    Newlines are preserved — collapsed, never removed. The FNOL heuristic reader
    matches `Label: value` anchored to the start of a line, so a reader that
    flattened its output to one long line would produce text nothing can match.
    """
    # Line endings first. PDFium emits `\r\n`, and a stray `\r` survives into the
    # evidence snippet an officer is shown and into the text a highlight is matched
    # against — where it reads as a mismatch rather than as a line break.
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    without_controls = _CONTROL_RE.sub("", unified)
    collapsed = _HORIZONTAL_WHITESPACE_RE.sub(" ", without_controls)
    collapsed = _BLANK_RUN_RE.sub("\n\n", collapsed)
    # Trailing spaces before a newline defeat the label reader's `$` anchor.
    collapsed = re.sub(r" +\n", "\n", collapsed)
    return collapsed.strip()[:budget]


__all__ = [
    "MAX_TEXT_CHARACTERS",
    "PAGE_SEPARATOR",
    "ExtractedDocumentText",
    "ExtractedPage",
    "clean_text",
]
