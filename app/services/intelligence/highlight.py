"""Turning a cited passage into something a viewer can draw.

When an officer clicks "where did this come from" on a field, the answer has three
levels of precision, and this module returns as much of it as the document allows:

1. **Which page**, always — from the passage's character offsets and the page spans
   the reader recorded.
2. **Which text**, always — the model's quoted evidence when it can be located
   inside the passage, otherwise the whole passage. Preferring the quote matters:
   highlighting a 320-token passage to show where one policy number came from is a
   wall of colour that answers nothing.
3. **Which rectangles**, for PDFs — the boxes to draw over the page image.

Level 3 is where the difficulty is, and it is worth stating why the obvious approach
does not work. The page text is produced by PDFium and the word geometry by
`pdfplumber` over `pdfminer.six`; two engines reading the same page produce the same
words in the same order but *not* the same character offsets, because they disagree
about where spaces and line breaks belong. So a character offset cannot be handed
from one to the other.

Matching is therefore done on the **word sequence**, which the two engines do agree
on: the quoted text is reduced to comparable tokens and located as a run inside the
page's word list. That is engine-agnostic, survives the reader being swapped, and
degrades honestly — a passage whose words cannot be found returns levels 1 and 2 with
no rectangles, and the viewer can fall back to searching its own text layer.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Characters of highlight. Beyond this the answer to "where did this come from" has
#: stopped being an answer.
MAX_HIGHLIGHT_CHARACTERS = 600

#: Words on a page that will be scanned for a match. A page holding more than this is
#: a data table, and the match would be arbitrary.
_MAX_PAGE_WORDS = 4_000

#: A run this short is a coincidence rather than a location — "the", "of", "and"
#: appear on every page of every document.
_MIN_MATCH_TOKENS = 2

#: Share of the quoted text's tokens that must be found for the match to be offered.
_MIN_MATCH_RATIO = 0.5

#: Vertical distance within which two words are treated as being on one line, as a
#: fraction of the line height. Words on one line become one rectangle.
_LINE_TOLERANCE = 0.6

_TOKEN_RE = re.compile(r"[0-9a-z]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class HighlightRect:
    """One rectangle, in PDF points from the top-left of the page.

    `page_width` and `page_height` travel with every rectangle so a viewer can scale
    to whatever size it is rendering at without a second request telling it how big
    the page is.
    """

    page_number: int
    x0: float
    top: float
    x1: float
    bottom: float
    page_width: float
    page_height: float


@dataclass(frozen=True, slots=True)
class Highlight:
    """Where a cited value can be seen in its document."""

    page_number: int | None
    section_label: str | None
    text: str
    #: Offsets into the document's stored text — the same coordinate system the
    #: passage's own offsets are in, so a text viewer can use them directly.
    char_start: int
    char_end: int
    #: The same span, relative to the start of its page.
    page_char_start: int | None
    page_char_end: int | None
    rects: tuple[HighlightRect, ...] = ()
    #: Set when rectangles could not be resolved, so the caller can say why rather
    #: than silently showing a page with nothing marked on it.
    note: str | None = None


def page_for_offset(
    page_offsets: list[list[int]] | None, offset: int
) -> tuple[int, int, int] | None:
    """`(page_index, page_start, page_end)` for the page containing `offset`.

    `page_index` is 0-based into `page_offsets`; the caller maps it to a page number,
    because the reader knows whether page 3 of its list is page 3 of the file.
    """
    if not page_offsets:
        return None
    for index, offsets in enumerate(page_offsets):
        if len(offsets) != 2:
            continue
        start, end = offsets
        if start <= offset < end:
            return index, start, end
    # Past the end of the last page: the closest true answer is the last page.
    for index in range(len(page_offsets) - 1, -1, -1):
        if len(page_offsets[index]) == 2:
            start, end = page_offsets[index]
            return index, start, end
    return None


def locate_in_chunk(
    chunk_content: str, chunk_start: int, quote: str | None
) -> tuple[int, int, str]:
    """Narrow a passage to the quoted evidence inside it.

    Returns absolute `(start, end, text)`. Falls back to the passage itself — capped
    — when there is no quote or the quote cannot be found in it, which is the honest
    answer: the value came from this passage, and this is as precise as we can be.
    """
    if quote:
        located = _find_normalised(chunk_content, quote)
        if located is not None:
            start, end = located
            return chunk_start + start, chunk_start + end, chunk_content[start:end]

    text = chunk_content[:MAX_HIGHLIGHT_CHARACTERS]
    return chunk_start, chunk_start + len(text), text


def _find_normalised(haystack: str, needle: str) -> tuple[int, int] | None:
    """Locate `needle` in `haystack`, tolerating whitespace differences.

    A model asked to quote what it read returns the words, not the line breaks, so an
    exact `str.find` misses a quote that spans a line in the original.
    """
    trimmed = needle.strip()
    if not trimmed:
        return None

    direct = haystack.find(trimmed)
    if direct >= 0:
        return direct, direct + len(trimmed)

    # Build a whitespace-flexible pattern from the quote's own words.
    words = [re.escape(word) for word in trimmed.split()]
    if not words:
        return None
    pattern = re.compile(r"\s+".join(words), re.IGNORECASE)
    match = pattern.search(haystack)
    if match:
        return match.start(), match.end()

    # Last resort: the first line of the quote, which is what a model returns when it
    # truncates a multi-line field value.
    first = trimmed.splitlines()[0].strip()
    if first and first != trimmed:
        return _find_normalised(haystack, first)
    return None


def resolve_pdf_rects(
    content: bytes, *, page_index: int, text: str
) -> tuple[tuple[HighlightRect, ...], str | None]:
    """Rectangles covering `text` on one page of a PDF. `(rects, note)`.

    Never raises: a PDF that cannot be re-opened at evidence time returns no
    rectangles and a sentence, because the page number and the quoted text are still
    a useful answer on their own.
    """
    tokens = _tokens(text)
    if len(tokens) < _MIN_MATCH_TOKENS:
        return (), "The quoted text is too short to locate on the page."

    try:
        import pdfplumber
    except ImportError:  # pragma: no cover — the dependency is declared
        return (), "Highlight rendering is not available in this deployment."

    try:
        with pdfplumber.open(io.BytesIO(content)) as document:
            if page_index >= len(document.pages):
                return (), "The cited page is no longer in this document."
            page = document.pages[page_index]
            words = page.extract_words(use_text_flow=True)[:_MAX_PAGE_WORDS]
            width = float(page.width)
            height = float(page.height)
    except Exception as exc:
        logger.info("highlight_page_unreadable", error=type(exc).__name__)
        return (), "This document's pages could not be measured for highlighting."

    matched = _match_words(words, tokens)
    if not matched:
        return (), "The quoted text could not be located on the page."

    return _rects_from_words(matched, page_number=page_index + 1, width=width, height=height), None


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def _match_words(words: list[dict[str, Any]], needle: list[str]) -> list[dict[str, Any]]:
    """The best run of `words` matching `needle` in order.

    A run rather than an exact subsequence because the two engines split hyphenated
    and punctuated words differently — `CP-2026-4471` is one word to one and three to
    the other — so an all-or-nothing match would fail on exactly the values that
    matter most here.

    **Longest wins, and among equals the one that skipped nothing.** Both halves of
    that rule are load-bearing, and the second was learned from a real highlight. A
    claim form prints a section heading above the field it heads:

        POLICY
        Policy number:            CP-4471-88210

    Searching for "Policy number: CP-4471-88210" from the *heading* reaches full
    length by skipping one token — the heading's `POLICY`, then `number`, `cp`,
    `4471`, `88210` — so a first-full-match-wins search stopped there and drew two
    rectangles: one over the heading, and one over the value line with its first word
    missing. Preferring the run that skipped nothing finds the three words actually
    quoted, on the one line they sit on.
    """
    page_tokens: list[tuple[int, str]] = []
    for position, word in enumerate(words):
        for token in _tokens(str(word.get("text", ""))):
            page_tokens.append((position, token))

    if not page_tokens:
        return []

    best: list[int] = []
    best_skips = 0

    for offset in range(len(page_tokens)):
        if page_tokens[offset][1] != needle[0]:
            continue

        # Record the token indices actually consumed rather than a length. The
        # interloper tolerance below can advance the cursor by two, so a length is not
        # enough to reconstruct which tokens matched — and reconstructing it wrongly
        # draws a rectangle over the start of the following line.
        consumed: list[int] = []
        skips = 0
        cursor = offset
        for token in needle:
            if cursor < len(page_tokens) and page_tokens[cursor][1] == token:
                consumed.append(cursor)
                cursor += 1
            elif cursor + 1 < len(page_tokens) and page_tokens[cursor + 1][1] == token:
                # Tolerate one interloper: a page number or a stray glyph sitting
                # between two words of the quote.
                consumed.append(cursor + 1)
                cursor += 2
                skips += 1
            else:
                break

        if not best or (len(consumed), -skips) > (len(best), -best_skips):
            best, best_skips = consumed, skips
        if len(best) == len(needle) and best_skips == 0:
            # Every token, contiguously. Nothing later can beat that, and the first
            # such run is the earliest place the quote actually appears on the page.
            break

    if len(best) < _MIN_MATCH_TOKENS or len(best) / len(needle) < _MIN_MATCH_RATIO:
        return []

    positions = {page_tokens[index][0] for index in best}
    return [word for index, word in enumerate(words) if index in positions]


def _rects_from_words(
    words: list[dict[str, Any]], *, page_number: int, width: float, height: float
) -> tuple[HighlightRect, ...]:
    """One rectangle per line of matched words.

    Per line rather than one box round everything: a quote spanning two lines would
    otherwise be drawn as a rectangle covering the whole width of both, including the
    text between them that the quote does not contain.
    """
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(
        words, key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))
    ):
        top = float(word.get("top", 0))
        bottom = float(word.get("bottom", top))
        tolerance = max(1.0, (bottom - top) * _LINE_TOLERANCE)
        if lines and abs(float(lines[-1][0].get("top", 0)) - top) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])

    rects: list[HighlightRect] = []
    for line in lines:
        rects.append(
            HighlightRect(
                page_number=page_number,
                x0=min(float(word.get("x0", 0)) for word in line),
                top=min(float(word.get("top", 0)) for word in line),
                x1=max(float(word.get("x1", 0)) for word in line),
                bottom=max(float(word.get("bottom", 0)) for word in line),
                page_width=width,
                page_height=height,
            )
        )
    return tuple(rects)


__all__ = [
    "MAX_HIGHLIGHT_CHARACTERS",
    "Highlight",
    "HighlightRect",
    "locate_in_chunk",
    "page_for_offset",
    "resolve_pdf_rects",
]
