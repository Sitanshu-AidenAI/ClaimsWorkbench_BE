"""Finding a value in a document, and the order the two searches run in.

The order is the point of this file. Grounding in the cited passage first and
searching the whole document second is what makes a highlight land on the
occurrence the model actually read rather than on the first one in the file — and
the difference only shows up when a value appears more than once, which on a
claim document it very often does.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest

from app.services.extraction.locate import (
    EvidenceLocator,
    _find_all,
    _snippet,
    rect_from_dict,
    rect_to_dict,
)
from app.services.intelligence.highlight import HighlightRect

TEXT = (
    "SURVEY REPORT\n"
    "Estimated loss: GBP 4,000\n"
    "The insured reports damage to stock.\n"
    "A second mention of GBP 4,000 appears here.\n"
)


class FakeDocument:
    def __init__(
        self,
        *,
        text: str = TEXT,
        content_type: str = "text/plain",
        pages: list[list[int]] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.filename = "survey.pdf"
        self.content_type = content_type
        self.storage_key = "fnol/X/abc"
        self.extracted_text = text
        self.page_offsets = pages if pages is not None else [[0, len(text)]]
        self.page_count = 1


class FakeChunk:
    def __init__(self, *, content: str, start: int, page: int | None = 1) -> None:
        self.id = uuid.uuid4()
        self.content = content
        self.char_start = start
        self.char_end = start + len(content)
        self.page_number = page
        self.section_label = None


class FakeDocuments:
    def __init__(self, content: bytes | None = None, *, fail: bool = False) -> None:
        self._content = content or b""
        self._fail = fail
        self.fetches = 0

    async def fetch(self, key: str) -> bytes:
        del key
        self.fetches += 1
        if self._fail:
            raise RuntimeError("the object is gone")
        return self._content


def build_pdf(lines: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    offset = 800
    for line in lines:
        document.drawString(60, offset, line)
        offset -= 20
    document.showPage()
    document.save()
    return buffer.getvalue()


class TestFindAll:
    def test_every_occurrence_is_found_in_order(self) -> None:
        spans = _find_all(TEXT, "GBP 4,000", limit=10)
        assert len(spans) == 2
        assert spans[0][0] < spans[1][0]
        assert TEXT[spans[0][0] : spans[0][1]] == "GBP 4,000"

    def test_whitespace_differences_do_not_defeat_a_match(self) -> None:
        """A reader emits the page's line breaks; a model returns the words.

        An exact `str.find` misses every value that happened to wrap, which on a
        claim form is most of the long ones.
        """
        haystack = "Loss location: Unit 7\nHarborview Estate, Hull"
        spans = _find_all(haystack, "Unit 7 Harborview Estate", limit=5)
        assert len(spans) == 1

    def test_a_one_character_needle_is_refused(self) -> None:
        assert _find_all(TEXT, "G", limit=5) == []

    def test_the_limit_is_honoured(self) -> None:
        assert len(_find_all("a b " * 40, "a b", limit=3)) == 3

    def test_a_snippet_carries_context_and_marks_where_it_was_cut(self) -> None:
        long_text = ("filler " * 40) + "the value" + (" filler" * 40)
        start = long_text.index("the value")
        snippet = _snippet(long_text, start, start + len("the value"))
        assert "the value" in snippet
        assert snippet.startswith("… ")
        assert snippet.endswith(" …")


class TestResolveEvidence:
    async def test_a_grounded_citation_narrows_the_passage_to_the_quote(self) -> None:
        document = FakeDocument()
        start = TEXT.index("Estimated loss")
        chunk = FakeChunk(content=TEXT[start : start + 60], start=start)
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]

        location = await locator.resolve_evidence(
            document,  # type: ignore[arg-type]
            chunk,  # type: ignore[arg-type]
            quote="GBP 4,000",
        )

        assert location.strategy == "text-only"
        assert location.text == "GBP 4,000"
        assert location.char_start == TEXT.index("GBP 4,000")
        assert location.page_number == 1

    async def test_the_first_occurrence_is_not_assumed_when_a_passage_is_cited(self) -> None:
        """The failure the reference implementation has, asserted as a difference.

        Searching the document for the value returns the *first* "GBP 4,000".
        Grounding in the cited passage returns the second one — the one the model
        was actually shown. On a claim document where a figure appears in a
        summary and again in a schedule, that is the whole answer.
        """
        document = FakeDocument()
        second = TEXT.index("GBP 4,000", TEXT.index("GBP 4,000") + 1)
        chunk = FakeChunk(content=TEXT[second - 20 : second + 20], start=second - 20)
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]

        location = await locator.resolve_evidence(
            document,  # type: ignore[arg-type]
            chunk,  # type: ignore[arg-type]
            quote="GBP 4,000",
        )
        assert location.char_start == second

    async def test_a_lost_passage_falls_back_to_searching_the_document(self) -> None:
        """Which happens after a re-index, and is exactly when an answer is wanted."""
        document = FakeDocument()
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]

        location = await locator.resolve_evidence(
            document,  # type: ignore[arg-type]
            None,
            quote=None,
            value="GBP 4,000",
        )
        assert location.strategy == "document-search"
        assert location.char_start == TEXT.index("GBP 4,000")
        assert location.note is not None

    async def test_a_value_with_nothing_to_search_for_says_so(self) -> None:
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]
        location = await locator.resolve_evidence(
            FakeDocument(),  # type: ignore[arg-type]
            None,
            quote=None,
            value=None,
        )
        assert location.strategy == "none"
        assert location.rects == []

    async def test_a_non_pdf_returns_text_and_says_why_there_is_no_geometry(self) -> None:
        """A normal answer, not an error.

        A Word document, a spreadsheet and the notification body have no page
        layout. The page label and the exact text are enough for a viewer to mark
        up its own rendered text, which is what the frontend does for them.
        """
        document = FakeDocument(content_type="text/plain")
        chunk = FakeChunk(content=TEXT[:60], start=0)
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]

        location = await locator.resolve_evidence(
            document,  # type: ignore[arg-type]
            chunk,  # type: ignore[arg-type]
            quote="SURVEY REPORT",
        )
        assert location.strategy == "text-only"
        assert location.rects == []
        assert "no page layout" in (location.note or "")

    async def test_a_file_that_cannot_be_read_still_answers_with_page_and_text(self) -> None:
        document = FakeDocument(content_type="application/pdf")
        chunk = FakeChunk(content=TEXT[:60], start=0)
        locator = EvidenceLocator(FakeDocuments(fail=True))  # type: ignore[arg-type]

        location = await locator.resolve_evidence(
            document,  # type: ignore[arg-type]
            chunk,  # type: ignore[arg-type]
            quote="SURVEY REPORT",
        )
        assert location.text == "SURVEY REPORT"
        assert location.page_number == 1
        assert location.rects == []
        assert "could not be read" in (location.note or "")

    async def test_a_real_pdf_yields_rectangles_at_the_drawn_coordinates(self) -> None:
        lines = ["SURVEY REPORT", "Policy number: CP-2026-4471", "Estimated loss: GBP 128,000"]
        text = "\n".join(lines)
        content = build_pdf(lines)

        document = FakeDocument(text=text, content_type="application/pdf")
        chunk = FakeChunk(content=text, start=0)
        locator = EvidenceLocator(FakeDocuments(content))  # type: ignore[arg-type]

        location = await locator.resolve_evidence(
            document,  # type: ignore[arg-type]
            chunk,  # type: ignore[arg-type]
            quote="Policy number: CP-2026-4471",
        )

        assert location.strategy == "chunk-grounded"
        assert location.rects, location.note
        rect = location.rects[0]
        assert rect.page_number == 1
        # Drawn at x=60; pdfplumber measures from the left edge.
        assert rect.x0 == pytest.approx(60, abs=2)
        assert rect.page_width > 0
        assert rect.page_height > 0


class TestLocateInDocument:
    async def test_every_occurrence_is_returned_with_its_page(self) -> None:
        document = FakeDocument()
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]

        found = await locator.locate_in_document(document, "GBP 4,000")  # type: ignore[arg-type]

        assert len(found) == 2
        assert all(occurrence.page_number == 1 for occurrence in found)
        assert all("GBP 4,000" in occurrence.snippet for occurrence in found)

    async def test_a_document_with_no_text_returns_nothing(self) -> None:
        locator = EvidenceLocator(FakeDocuments())  # type: ignore[arg-type]
        assert await locator.locate_in_document(FakeDocument(text=""), "x") == []  # type: ignore[arg-type]

    async def test_a_pdf_is_fetched_once_for_all_occurrences(self) -> None:
        """Not once per occurrence: a 30MB survey report parsed twenty times is a timeout."""
        lines = ["GBP 4,000 appears here", "and GBP 4,000 appears again"]
        text = "\n".join(lines)
        documents = FakeDocuments(build_pdf(lines))
        document = FakeDocument(text=text, content_type="application/pdf")
        locator = EvidenceLocator(documents)  # type: ignore[arg-type]

        found = await locator.locate_in_document(document, "GBP 4,000")  # type: ignore[arg-type]

        assert len(found) == 2
        assert documents.fetches == 1
        assert any(occurrence.rects for occurrence in found)


class TestRectSerialisation:
    def test_a_rectangle_round_trips_through_its_cached_form(self) -> None:
        """It is cached on the value row as JSON and read back on the next request."""
        rect = HighlightRect(
            page_number=4,
            x0=60.0,
            top=32.4,
            x1=220.7,
            bottom=44.4,
            page_width=595.0,
            page_height=842.0,
        )
        assert rect_from_dict(rect_to_dict(rect)) == rect

    def test_the_cached_shape_is_the_shape_the_viewer_is_given(self) -> None:
        """The frontend scales `x0 * zoom`, so a renamed key is a silent misdraw."""
        payload: dict[str, Any] = rect_to_dict(
            HighlightRect(
                page_number=1, x0=1.0, top=2.0, x1=3.0, bottom=4.0, page_width=5.0, page_height=6.0
            )
        )
        assert set(payload) == {
            "page_number",
            "x0",
            "top",
            "x1",
            "bottom",
            "page_width",
            "page_height",
        }
