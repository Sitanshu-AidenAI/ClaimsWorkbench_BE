"""Finding a value in a document, so a viewer can draw a box round it.

Three questions, and they are different enough to be separate entry points.

**"Where is this stored citation?"** — `resolve_citation`. The narrowing has
already happened: a citation row carries the quote as its own document writes it
and the offset it sits at, so all that is left is mapping the offset to a page and
measuring that text on that page. This is the hot path — it is what an officer
clicking a source chip calls — and it is the cheapest, because nothing has to be
searched for.

**"Where is the value this field cites?"** — `resolve_evidence`. The passage is
known but the quote has not been narrowed against it yet, so the answer is
grounded here: narrow the passage to the model's quote, map the offset to a page,
and locate that text *on that page*. One answer, and it is the right one even when
the same words appear four times in the file.

**"Where else does this text appear?"** — `locate_in_document`. No passage, no
grounding, every occurrence. This is what a reviewer wants after the first
question has been answered and they are stepping through matches, and it is the
only thing available when a value has no citation at all — a figure an officer
typed, or one whose passage was replaced by a re-index.

The order matters. Searching the whole document first, which is the obvious
implementation and the one the reference system uses, answers the second question
and pretends it answered the first: it highlights the *first* occurrence of
"£4,000", which is very often not the one the model read. Grounding first and
falling back second gives the accurate answer where one exists and a useful one
everywhere else.

Nothing here raises. A PDF that cannot be re-opened, a page that has been removed
since extraction, a quote whose words have been re-flowed — each returns fewer
levels of precision and a sentence saying so, because the page number and the
quoted text remain a useful answer on their own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.models.fnol import FNOLDocument, FNOLDocumentChunk
from app.services.documents.service import DocumentProcessingService
from app.services.extraction.matching import NormalisedText, collapse
from app.services.intelligence.highlight import (
    HighlightRect,
    locate_in_chunk,
    page_for_offset,
    resolve_pdf_rects,
)

logger = get_logger(__name__)

PDF_CONTENT_TYPE = "application/pdf"

#: Occurrences returned by a document-wide search. Beyond this a reviewer is not
#: stepping through matches, they are reading the document.
MAX_OCCURRENCES = 50

#: Characters of context either side of an occurrence in the snippet.
_SNIPPET_PADDING = 90


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One place a piece of text appears in a document."""

    page_number: int | None
    char_start: int
    char_end: int
    snippet: str
    rects: tuple[HighlightRect, ...] = ()


@dataclass(slots=True)
class EvidenceLocation:
    """Where a cited value can be seen, at whatever precision the file allows."""

    page_number: int | None = None
    section_label: str | None = None
    text: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    rects: list[HighlightRect] = field(default_factory=list)
    #: `chunk-grounded` — located inside the passage the model cited.
    #: `document-search` — the passage was lost, so the value's own text was
    #: searched for instead. `text-only` — no geometry, which is the normal
    #: answer for a Word document. `none` — nothing to point at.
    strategy: str = "none"
    #: Why there are no rectangles, when there are none. Shown to the reviewer.
    note: str | None = None


class EvidenceLocator:
    """Resolves highlights against the stored bytes of a document."""

    def __init__(self, documents: DocumentProcessingService) -> None:
        self._documents = documents

    async def resolve_citation(
        self,
        document: FNOLDocument,
        *,
        quote: str | None,
        char_start: int | None,
        page_number: int | None,
        section_label: str | None = None,
        grounded: bool = True,
    ) -> EvidenceLocation:
        """Rectangles for a citation whose text and offset are already recorded.

        The citation row is the authority on *what* to mark and *where in the
        document's text* it sits — both were settled when the value was extracted.
        This resolves the one thing that cannot be stored: the geometry, which
        needs the file's bytes.

        `char_start` is preferred over `page_number` for finding the page, because
        an offset survives a re-read that adds a page and a stored page number does
        not. The stored number is the fallback for a document with no recorded page
        spans, which is every non-PDF.
        """
        located = (
            page_for_offset(document.page_offsets, char_start) if char_start is not None else None
        )
        page = (located[0] + 1) if located else page_number

        location = EvidenceLocation(
            page_number=page,
            section_label=section_label,
            text=quote,
            char_start=char_start,
            char_end=(char_start + len(quote)) if (char_start is not None and quote) else None,
            strategy="chunk-grounded" if grounded else "document-search",
        )

        if not quote:
            location.strategy = "none"
            location.note = "This citation has no recorded text to mark."
            return location

        if document.content_type != PDF_CONTENT_TYPE:
            location.note = "This document has no page layout; highlight the quoted text instead."
            location.strategy = "text-only"
            return location

        page_index = located[0] if located else (page - 1 if page else None)
        if page_index is None or page_index < 0:
            location.note = "This document's page positions were not recorded."
            location.strategy = "text-only"
            return location

        content = await self._fetch(document)
        if content is None:
            location.note = "The stored file could not be read to draw the highlight."
            return location

        rects, note = await asyncio.to_thread(
            resolve_pdf_rects, content, page_index=page_index, text=quote
        )
        location.rects = list(rects)
        location.note = note
        return location

    async def resolve_evidence(
        self,
        document: FNOLDocument,
        chunk: FNOLDocumentChunk | None,
        *,
        quote: str | None,
        value: str | None = None,
    ) -> EvidenceLocation:
        """The best answer to "where did this value come from" for one document.

        Grounded in `chunk` when there is one. Falls back to searching the
        document for `value` when the passage has gone — which happens after a
        re-index, and is exactly when a reviewer most wants an answer rather than
        an apology.
        """
        if chunk is not None:
            return await self._grounded(document, chunk, quote=quote)

        needle = (quote or value or "").strip()
        if not needle:
            return EvidenceLocation(
                note="This value has no recorded source to point at.", strategy="none"
            )

        found = await self.locate_in_document(document, needle, limit=1)
        if not found:
            return EvidenceLocation(
                note="This value could not be found in the document as it is stored now.",
                strategy="none",
            )

        first = found[0]
        return EvidenceLocation(
            page_number=first.page_number,
            text=first.snippet,
            char_start=first.char_start,
            char_end=first.char_end,
            rects=list(first.rects),
            strategy="document-search",
            note=(
                None
                if first.rects
                else "The passage this was read from has been re-indexed; "
                "this is the first place the value now appears."
            ),
        )

    async def _grounded(
        self, document: FNOLDocument, chunk: FNOLDocumentChunk, *, quote: str | None
    ) -> EvidenceLocation:
        start, end, text = locate_in_chunk(chunk.content, chunk.char_start, quote)
        located = page_for_offset(document.page_offsets, start)
        page_number = (located[0] + 1) if located else chunk.page_number

        location = EvidenceLocation(
            page_number=page_number,
            section_label=chunk.section_label,
            text=text,
            char_start=start,
            char_end=end,
            strategy="chunk-grounded",
        )

        if document.content_type != PDF_CONTENT_TYPE:
            # A Word document, a spreadsheet or the notification body has no page
            # geometry to draw on. The page label and the exact text are the
            # honest answer, and are enough for a viewer to mark up its own text.
            location.note = "This document has no page layout; highlight the quoted text instead."
            location.strategy = "text-only"
            return location

        if located is None:
            location.note = "This document's page positions were not recorded."
            location.strategy = "text-only"
            return location

        content = await self._fetch(document)
        if content is None:
            location.note = "The stored file could not be read to draw the highlight."
            return location

        rects, note = await asyncio.to_thread(
            resolve_pdf_rects, content, page_index=located[0], text=text
        )
        location.rects = list(rects)
        location.note = note
        return location

    async def locate_in_document(
        self, document: FNOLDocument, needle: str, *, limit: int = MAX_OCCURRENCES
    ) -> list[Occurrence]:
        """Every place `needle` appears in a document's stored text.

        Matching is whitespace-tolerant against the text the reader produced,
        which is the same text the passages were cut from — so an offset returned
        here is in the same coordinate system as everything else in the module.
        """
        text = document.extracted_text or ""
        if not text or not needle.strip():
            return []

        spans = _find_all(text, needle, limit=min(limit, MAX_OCCURRENCES))
        if not spans:
            return []

        pages = document.page_offsets
        content = await self._fetch(document) if document.content_type == PDF_CONTENT_TYPE else None

        occurrences: list[Occurrence] = []
        for start, end in spans:
            located = page_for_offset(pages, start)
            rects: tuple[HighlightRect, ...] = ()
            if content is not None and located is not None:
                rects, _ = await asyncio.to_thread(
                    resolve_pdf_rects, content, page_index=located[0], text=text[start:end]
                )
            occurrences.append(
                Occurrence(
                    page_number=(located[0] + 1) if located else None,
                    char_start=start,
                    char_end=end,
                    snippet=_snippet(text, start, end),
                    rects=rects,
                )
            )
        return occurrences

    async def _fetch(self, document: FNOLDocument) -> bytes | None:
        try:
            return await self._documents.fetch(document.storage_key)
        except Exception as exc:
            logger.info(
                "evidence_document_unreadable",
                document_id=str(document.id),
                error=type(exc).__name__,
            )
            return None


def _find_all(haystack: str, needle: str, *, limit: int) -> list[tuple[int, int]]:
    """Locate every occurrence, tolerating whitespace and case differences.

    A value stored as the model returned it rarely matches the document byte for
    byte: a reader emits the line breaks the page had, and a model returns the
    words. Delegated to `app.services.extraction.matching`, which is the one
    definition of "the same text" in this module and in the corroboration pass —
    two definitions would mean a value the citation writer found and the
    occurrence search cannot.
    """
    if len(collapse(needle)) < 2:
        return []
    return NormalisedText.of(haystack).find_all(needle, limit=limit)


def _snippet(text: str, start: int, end: int) -> str:
    """The occurrence with enough around it to recognise where it sits."""
    left = max(0, start - _SNIPPET_PADDING)
    right = min(len(text), end + _SNIPPET_PADDING)
    prefix = "… " if left > 0 else ""
    suffix = " …" if right < len(text) else ""
    return f"{prefix}{text[left:right].strip()}{suffix}"


def rect_to_dict(rect: HighlightRect) -> dict[str, Any]:
    """A rectangle as it is cached on a value row and served to a viewer."""
    return {
        "page_number": rect.page_number,
        "x0": rect.x0,
        "top": rect.top,
        "x1": rect.x1,
        "bottom": rect.bottom,
        "page_width": rect.page_width,
        "page_height": rect.page_height,
    }


def rect_from_dict(payload: dict[str, Any]) -> HighlightRect:
    """The inverse, for a cached rectangle read back off a value row."""
    return HighlightRect(
        page_number=int(payload["page_number"]),
        x0=float(payload["x0"]),
        top=float(payload["top"]),
        x1=float(payload["x1"]),
        bottom=float(payload["bottom"]),
        page_width=float(payload["page_width"]),
        page_height=float(payload["page_height"]),
    )


__all__ = [
    "MAX_OCCURRENCES",
    "PDF_CONTENT_TYPE",
    "EvidenceLocation",
    "EvidenceLocator",
    "Occurrence",
    "rect_from_dict",
    "rect_to_dict",
]
