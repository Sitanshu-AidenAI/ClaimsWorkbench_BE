"""Chunking and highlighting.

These two modules are tested together because they are two halves of one promise: a
passage records where it came from, and the highlighter turns that back into something
a person can see. The invariant that connects them —

    chunk.content == document_text[chunk.char_start:chunk.char_end]

— is asserted in almost every test here, because everything visible to a claims officer
rests on it. If it breaks, the system does not error; it shows the wrong part of the
wrong page, confidently.
"""

from __future__ import annotations

import io
from itertools import pairwise

import pytest

from app.services.documents.text import extract_text
from app.services.intelligence.chunking import (
    Region,
    chunk_document,
    content_hash,
    regions_from_pages,
)
from app.services.intelligence.highlight import (
    MAX_HIGHLIGHT_CHARACTERS,
    locate_in_chunk,
    page_for_offset,
    resolve_pdf_rects,
)


def build_pdf(pages: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    for body in pages:
        offset = 800
        for line in body.splitlines():
            document.drawString(60, offset, line)
            offset -= 20
        document.showPage()
    document.save()
    return buffer.getvalue()


SURVEY_PAGES = [
    "SURVEY REPORT\nUnit 7 Harborview Estate\nPrepared for Harborview Logistics Ltd",
    "Policy number: CP-2026-4471\nDate of loss: 08 March 2026\nCause: escape of water",
    "Estimated loss: GBP 128,000\nRepair estimate: GBP 96,400\nBusiness interruption: yes",
]


@pytest.fixture
def survey() -> tuple[bytes, str, list[Region]]:
    """A three-page PDF, its extracted text, and its page regions."""
    content = build_pdf(SURVEY_PAGES)
    extracted = extract_text("application/pdf", content)
    regions = regions_from_pages(
        extracted.page_offsets,
        [page.label for page in extracted.pages],
        [page.number for page in extracted.pages],
    )
    return content, extracted.text, regions


class TestChunkingInvariant:
    def test_every_passage_is_an_exact_slice_of_the_document(
        self, survey: tuple[bytes, str, list[Region]]
    ) -> None:
        _, text, regions = survey
        chunks, _ = chunk_document(
            text, regions=regions, target_chars=120, overlap_chars=30, max_chunks=100
        )
        assert chunks
        for chunk in chunks:
            assert chunk.content == text[chunk.char_start : chunk.char_end]

    def test_overlap_moves_the_start_back_and_keeps_the_slice_exact(self) -> None:
        # The reason overlap is implemented as a backwards start rather than as a
        # prepended tail: a concatenated passage is no longer a slice, and every
        # citation built from its offsets would point at the wrong text.
        text = "\n".join(f"Line {index} of the narrative." for index in range(40))
        chunks, _ = chunk_document(text, target_chars=100, overlap_chars=40, max_chunks=100)
        assert len(chunks) > 2
        for chunk in chunks:
            assert chunk.content == text[chunk.char_start : chunk.char_end]
        # Consecutive passages genuinely overlap.
        assert any(later.char_start < earlier.char_end for earlier, later in pairwise(chunks))

    def test_offsets_are_monotonic(self) -> None:
        text = "\n".join(f"Paragraph {index}." for index in range(60))
        chunks, _ = chunk_document(text, target_chars=90, overlap_chars=0, max_chunks=100)
        for chunk in chunks:
            assert chunk.char_start < chunk.char_end
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


class TestChunkingBoundaries:
    def test_passages_do_not_straddle_pages(self, survey: tuple[bytes, str, list[Region]]) -> None:
        # A passage spanning two pages cannot say which page it is from, and a citation
        # that names the wrong page is worse than one that names none.
        _, text, regions = survey
        chunks, _ = chunk_document(
            text, regions=regions, target_chars=100, overlap_chars=0, max_chunks=100
        )
        for chunk in chunks:
            assert chunk.page_from == chunk.page_to

    def test_short_pages_are_packed_rather_than_becoming_a_passage_each(self) -> None:
        # Four two-word pages should not become four passages; they should become one
        # that honestly reports spanning pages 1 to 4.
        text = "One.\nTwo.\nThree.\nFour."
        regions = [
            Region(number=index + 1, label=None, start=start, end=start + 5)
            for index, start in enumerate((0, 5, 10, 17))
        ]
        chunks, _ = chunk_document(
            text, regions=regions, target_chars=400, overlap_chars=0, max_chunks=10
        )
        assert len(chunks) == 1
        assert (chunks[0].page_from, chunks[0].page_to) == (1, 4)

    def test_a_single_unbroken_run_is_hard_cut_rather_than_kept_whole(self) -> None:
        # What a badly extracted PDF page looks like: one enormous line. Without a hard
        # cut it becomes one passage the size of the document.
        text = "x" * 5_000
        chunks, _ = chunk_document(text, target_chars=200, overlap_chars=0, max_chunks=100)
        assert len(chunks) > 10
        assert max(len(chunk.content) for chunk in chunks) <= 400

    def test_the_cap_truncates_and_says_so(self) -> None:
        text = "\n".join(f"Line {index}." for index in range(500))
        chunks, truncated = chunk_document(text, target_chars=40, overlap_chars=0, max_chunks=5)
        assert truncated is True
        assert len(chunks) == 5

    def test_empty_text_yields_nothing(self) -> None:
        assert chunk_document("   \n  ", target_chars=100, overlap_chars=0, max_chunks=10) == (
            [],
            False,
        )

    def test_section_labels_travel_with_the_passage(self) -> None:
        text = "Description of loss\nWater ingress through a failed riser joint at Unit 7."
        regions = [Region(number=None, label="Description of loss", start=0, end=len(text))]
        chunks, _ = chunk_document(
            text, regions=regions, target_chars=400, overlap_chars=0, max_chunks=10
        )
        assert chunks[0].section_label == "Description of loss"
        assert chunks[0].page_number is None


class TestContentHash:
    def test_whitespace_differences_hash_the_same(self) -> None:
        # The same boilerplate paragraph arriving from two documents differs in
        # whitespace far more often than in words, and should embed once.
        assert content_hash("Policy   number:\nCP-2026-4471") == content_hash(
            "policy number: CP-2026-4471"
        )

    def test_different_words_hash_differently(self) -> None:
        assert content_hash("CP-2026-4471") != content_hash("CP-2026-4472")


class TestPageResolution:
    def test_an_offset_resolves_to_its_page(self, survey: tuple[bytes, str, list[Region]]) -> None:
        _, text, _ = survey
        extracted = extract_text("application/pdf", build_pdf(SURVEY_PAGES))
        offset = text.index("CP-2026-4471")
        located = page_for_offset(extracted.page_offsets, offset)
        assert located is not None
        # The policy number is on the second page of the fixture.
        assert located[0] == 1

    def test_no_pages_means_no_answer_rather_than_a_guess(self) -> None:
        assert page_for_offset(None, 10) is None
        assert page_for_offset([], 10) is None

    def test_an_offset_past_the_end_falls_back_to_the_last_page(self) -> None:
        located = page_for_offset([[0, 10], [10, 20]], 9_999)
        assert located == (1, 10, 20)


class TestLocateInChunk:
    def test_a_quote_narrows_the_highlight_to_itself(self) -> None:
        chunk = "Policy number: CP-2026-4471\nDate of loss: 08 March 2026"
        start, end, text = locate_in_chunk(chunk, 1_000, "CP-2026-4471")
        assert text == "CP-2026-4471"
        assert (start, end) == (
            1_000 + chunk.index("CP-2026-4471"),
            1_000 + chunk.index("CP-2026-4471") + 12,
        )

    def test_a_quote_spanning_a_line_break_is_still_found(self) -> None:
        # A model asked to quote what it read returns the words, not the line breaks.
        chunk = "Insured name:\nHarborview Logistics Ltd"
        _, _, text = locate_in_chunk(chunk, 0, "Insured name: Harborview Logistics Ltd")
        assert "Harborview" in text

    def test_no_quote_falls_back_to_the_passage_capped(self) -> None:
        chunk = "y" * (MAX_HIGHLIGHT_CHARACTERS * 2)
        start, end, text = locate_in_chunk(chunk, 0, None)
        assert len(text) == MAX_HIGHLIGHT_CHARACTERS
        assert (start, end) == (0, MAX_HIGHLIGHT_CHARACTERS)

    def test_an_unfindable_quote_falls_back_rather_than_failing(self) -> None:
        chunk = "Policy number: CP-2026-4471"
        _, _, text = locate_in_chunk(chunk, 0, "something the document never said")
        assert text == chunk


class TestPdfHighlighting:
    def test_a_quote_resolves_to_one_rectangle_per_line(
        self, survey: tuple[bytes, str, list[Region]]
    ) -> None:
        content, text, _ = survey
        extracted = extract_text("application/pdf", content)

        offset = text.index("Policy number: CP-2026-4471")
        located = page_for_offset(extracted.page_offsets, offset)
        assert located is not None

        rects, note = resolve_pdf_rects(
            content, page_index=located[0], text="Policy number: CP-2026-4471"
        )
        assert note is None
        assert len(rects) == 1
        rect = rects[0]
        # The fixture draws at x=60, and the page is A4 in points.
        assert rect.page_number == 2
        assert rect.x0 == pytest.approx(60.0, abs=1.0)
        assert rect.x1 > rect.x0
        assert rect.bottom > rect.top
        assert (rect.page_width, rect.page_height) == pytest.approx((595.0, 842.0), abs=2.0)

    def test_a_two_line_quote_becomes_two_rectangles(
        self, survey: tuple[bytes, str, list[Region]]
    ) -> None:
        # One box round both lines would cover the text between them, which the quote
        # does not contain.
        content, _, _ = survey
        rects, note = resolve_pdf_rects(
            content, page_index=1, text="Policy number: CP-2026-4471 Date of loss: 08 March 2026"
        )
        assert note is None
        assert len(rects) == 2
        assert rects[0].top < rects[1].top

    def test_a_heading_above_the_field_does_not_steal_the_highlight(self) -> None:
        """The bug that made a highlight look like it had jumped a line.

        A claim form heads a section with the same word its first field starts
        with, which is what a claim form does:

            POLICY
            Policy number:            CP-4471-88210

        Matching from the heading reaches full length by skipping one token — the
        heading's own word — so a search that stopped at the first full-length run
        drew two boxes: one over the heading, and one over the value line with its
        first word missing. The contiguous run is the right answer and this is that
        assertion.
        """
        content = build_pdf(["POLICY\nPolicy number: CP-4471-88210\nInsured name: Harborline"])

        rects, note = resolve_pdf_rects(content, page_index=0, text="Policy number: CP-4471-88210")

        assert note is None
        # One line, one box — and it starts at the left margin, which is where the
        # word "Policy" of the *field* sits rather than where the heading does.
        assert len(rects) == 1
        assert rects[0].x0 == pytest.approx(60.0, abs=1.0)
        # The heading is drawn 20pt above the field in the fixture, and is not in it.
        assert rects[0].top > 20

    def test_an_interloper_is_still_tolerated_when_there_is_no_clean_run(self) -> None:
        # The tolerance exists for a stray glyph between two words of a quote — a
        # footnote marker, a page number — and preferring clean runs must not have
        # removed it.
        content = build_pdf(["Estimated loss: GBP * 128,000 as advised"])

        rects, note = resolve_pdf_rects(content, page_index=0, text="GBP 128,000")

        assert note is None
        assert len(rects) == 1

    def test_text_that_is_not_on_the_page_returns_no_rectangles_and_a_reason(
        self, survey: tuple[bytes, str, list[Region]]
    ) -> None:
        content, _, _ = survey
        rects, note = resolve_pdf_rects(
            content, page_index=0, text="a phrase that appears nowhere in this document"
        )
        assert rects == ()
        assert note is not None and "could not be located" in note

    def test_a_page_beyond_the_document_is_reported_not_raised(
        self, survey: tuple[bytes, str, list[Region]]
    ) -> None:
        content, _, _ = survey
        rects, note = resolve_pdf_rects(content, page_index=99, text="Policy number")
        assert rects == ()
        assert note is not None

    def test_an_unreadable_file_degrades_to_page_and_text(self) -> None:
        # The page number and the quote are still a useful answer on their own.
        rects, note = resolve_pdf_rects(b"not a pdf at all", page_index=0, text="Policy number")
        assert rects == ()
        assert note is not None

    def test_a_quote_too_short_to_locate_is_refused(
        self, survey: tuple[bytes, str, list[Region]]
    ) -> None:
        content, _, _ = survey
        rects, note = resolve_pdf_rects(content, page_index=0, text="7")
        assert rects == ()
        assert note is not None and "too short" in note
