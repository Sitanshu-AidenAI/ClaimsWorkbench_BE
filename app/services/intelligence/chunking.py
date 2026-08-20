"""Splitting a document's text into citable passages.

Pure functions, no I/O, no configuration lookups — everything arrives as an
argument. That matters because the offset arithmetic in here is what a highlight is
built from, and it has to be testable without a database, a provider or a file.

**The invariant this module exists to hold:**

    chunk.content == document_text[chunk.char_start : chunk.char_end]

Everything downstream depends on it. The evidence endpoint resolves a highlight by
taking those offsets, finding which page they fall in, and locating the text on that
page; if a chunk's content were assembled rather than sliced, the offsets would
point somewhere else and the officer would be shown the wrong part of the document.
It is why overlap is implemented by moving a chunk's *start* backwards rather than
by prepending the previous chunk's tail, which is the obvious implementation and
quietly breaks the invariant.

**No `semchunk`, no `tiktoken`** — a deviation from what the IIF pipeline uses, and
from this work's own plan. Two reasons:

* `tiktoken` fetches its vocabulary from a CDN on first use, so an air-gapped worker
  fails on its first document at whatever hour that happens. It would be paying that
  risk to compute a number that only feeds a size heuristic: the prompt budget in
  `AISettings.max_input_characters` is already denominated in characters, so nothing
  downstream needs an exact token count. `token_count` here is an estimate, named as
  one.
* `semchunk`'s value is offsets, and offsets are precisely the part that must be
  exactly right. ~80 lines that this codebase owns and tests is a better trade than
  a dependency whose offset semantics we would have to trust and verify anyway.

Passages are cut **within a page or section**, never across one, so a citation can
name where it came from without qualification. The exception is a run of short
pages — a title page, a two-line covering note — which are packed together rather
than becoming a chunk each; those carry `page_from`/`page_to` and the honest answer
that they span a range.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import pairwise

#: Characters per token, for the estimate stored alongside each chunk. English prose
#: through a byte-pair encoder runs a little under four; this is used for budgeting
#: and reporting, never for truncating a prompt.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chunk:
    """One passage, and everything needed to cite it."""

    index: int
    content: str
    char_start: int
    char_end: int
    page_number: int | None
    page_from: int | None
    page_to: int | None
    section_label: str | None

    @property
    def token_estimate(self) -> int:
        return max(1, -(-len(self.content) // CHARS_PER_TOKEN))

    @property
    def content_hash(self) -> str:
        return content_hash(self.content)


@dataclass(frozen=True, slots=True)
class Region:
    """A span of the document that a passage may not be cut across.

    A PDF page, a Word section, a worksheet. `number` and `label` are what a
    citation shows.
    """

    number: int | None
    label: str | None
    start: int
    end: int


_WS_RE = re.compile(r"\s+")


def content_hash(text: str) -> str:
    """sha256 of the normalised text, so identical boilerplate embeds once.

    Normalised because the same paragraph arriving from two documents differs in
    whitespace far more often than it differs in words.
    """
    normalised = _WS_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalised.encode("utf-8", errors="replace")).hexdigest()


#: Where a passage may be cut, in descending order of preference. Each pattern is
#: applied to the text of one region; the first that yields more than one piece wins.
_SPLIT_PATTERNS = (
    # A blank line: a paragraph or table break.
    re.compile(r"\n\s*\n"),
    # A single newline: a line of a form, or a row of a rendered table. Cutting here
    # keeps `Label: value` whole, which is what the deterministic reader needs.
    re.compile(r"\n"),
    # A sentence end followed by a space.
    re.compile(r"(?<=[.!?])\s+"),
)


def chunk_document(
    text: str,
    *,
    regions: list[Region] | None = None,
    target_chars: int,
    overlap_chars: int,
    max_chunks: int,
) -> tuple[list[Chunk], bool]:
    """Split `text` into passages. Returns `(chunks, was_truncated)`.

    `regions` are the page or section spans a passage may not straddle. When it is
    empty the whole text is one region, which is the right answer for a plain-text
    note and for any format with no pagination.
    """
    if not text.strip():
        return [], False

    spans = regions or [Region(number=None, label=None, start=0, end=len(text))]
    min_chars = max(1, target_chars // 4)

    chunks: list[Chunk] = []
    truncated = False
    #: Regions held back because they were too short to be a passage on their own.
    pending: list[Region] = []

    for region in spans:
        pending.append(region)
        held = sum(span.end - span.start for span in pending)
        if held < min_chars:
            # Too little text to cite on its own — carry it into the next region.
            continue

        for chunk in _split_group(
            text,
            pending,
            start_index=len(chunks),
            target_chars=target_chars,
            overlap_chars=overlap_chars,
        ):
            if len(chunks) >= max_chunks:
                truncated = True
                break
            chunks.append(chunk)
        pending = []

        if truncated:
            break

    if pending and not truncated:
        for chunk in _split_group(
            text,
            pending,
            start_index=len(chunks),
            target_chars=target_chars,
            overlap_chars=overlap_chars,
        ):
            if len(chunks) >= max_chunks:
                truncated = True
                break
            chunks.append(chunk)

    return chunks, truncated


def _split_group(
    text: str,
    regions: list[Region],
    *,
    start_index: int,
    target_chars: int,
    overlap_chars: int,
) -> list[Chunk]:
    """Cut one group of regions into passages."""
    start = regions[0].start
    end = regions[-1].end
    numbered = [region.number for region in regions if region.number is not None]
    labels = [region.label for region in regions if region.label]

    page_from = min(numbered) if numbered else None
    page_to = max(numbered) if numbered else None
    label = labels[0] if labels else None

    boundaries = _boundaries(text[start:end], offset=start, target_chars=target_chars)

    chunks: list[Chunk] = []
    cursor = start
    while cursor < end:
        stop = _next_stop(boundaries, cursor=cursor, limit=end, target_chars=target_chars)
        body = text[cursor:stop]
        if body.strip():
            chunks.append(
                Chunk(
                    index=start_index + len(chunks),
                    content=body,
                    char_start=cursor,
                    char_end=stop,
                    page_number=page_from,
                    page_from=page_from,
                    page_to=page_to,
                    section_label=label,
                )
            )
        if stop >= end:
            break
        # Overlap by moving the next passage's start backwards, snapped to a
        # boundary. Concatenating the previous tail instead would break the
        # content-equals-slice invariant this module exists to hold.
        #
        # The floor is the *current* cursor rather than the region's start, and that is
        # what guarantees the loop terminates. Backing up by `overlap_chars` from `stop`
        # can land on a boundary at or before where this passage began — which happens
        # whenever the overlap is wide relative to the spacing between boundaries — and
        # the next iteration then recomputes the same `stop` from an earlier cursor and
        # the two positions oscillate forever. Floored here, every passage starts
        # strictly after the last one, so the cut always advances.
        cursor = _overlap_start(
            boundaries, stop=stop, floor=cursor + 1, overlap_chars=overlap_chars
        )

    return chunks


def _boundaries(body: str, *, offset: int, target_chars: int) -> list[int]:
    """Absolute offsets a passage may start or end at, ascending.

    Always includes the region's own start and end, so a region with no internal
    structure is still cuttable.
    """
    positions = {0, len(body)}

    for pattern in _SPLIT_PATTERNS:
        for match in pattern.finditer(body):
            positions.add(match.end())
        if len([position for position in positions if 0 < position < len(body)]) >= 1:
            # A coarser split produced somewhere to cut; finer ones are only needed
            # when it did not.
            break

    ordered = sorted(positions)

    # A stretch with no boundary at all — a single unbroken run of text, which is
    # what a badly extracted PDF page looks like — is hard-cut on the target so one
    # 40,000-character line does not become one 40,000-character passage.
    filled: list[int] = []
    for previous, position in pairwise(ordered):
        filled.append(previous)
        gap = position - previous
        if gap > target_chars:
            filled.extend(range(previous + target_chars, position, target_chars))
    filled.append(ordered[-1])

    return [offset + position for position in filled]


def _next_stop(boundaries: list[int], *, cursor: int, limit: int, target_chars: int) -> int:
    """The furthest boundary that keeps the passage within `target_chars`."""
    ideal = cursor + target_chars
    candidates = [position for position in boundaries if cursor < position <= min(ideal, limit)]
    if candidates:
        return max(candidates)

    # Nothing within budget: take the next boundary even though it overshoots,
    # because a passage has to end somewhere and a boundary is a better place than
    # the middle of a word.
    beyond = [position for position in boundaries if position > cursor]
    return min(beyond[0], limit) if beyond else limit


def _overlap_start(boundaries: list[int], *, stop: int, floor: int, overlap_chars: int) -> int:
    """Where the next passage begins, backed up by roughly `overlap_chars`.

    `floor` is a hard lower bound on the answer, and the caller passes the current
    cursor plus one so that the result is always forward of where the last passage
    began. Without that the returned position can precede the current cursor and the
    cut stops making progress — see the comment at the call site.
    """
    if overlap_chars <= 0:
        return stop
    target = max(floor, stop - overlap_chars)
    candidates = [position for position in boundaries if target <= position < stop]
    return min(candidates) if candidates else stop


def regions_from_pages(
    page_offsets: list[list[int]] | None,
    labels: list[str | None] | None = None,
    numbers: list[int] | None = None,
) -> list[Region]:
    """Build regions from the page spans a reader recorded.

    Page numbers come from the reader rather than from the list position, because a
    reader that dropped an empty page still knows what the surviving pages were
    called — and a citation reading "page 4" has to mean the fourth page of the file
    the officer opens, not the fourth page that happened to have text on it.
    """
    if not page_offsets:
        return []

    spans: list[Region] = []
    for index, offsets in enumerate(page_offsets):
        if len(offsets) != 2:
            continue
        start, end = offsets
        if end <= start:
            continue
        spans.append(
            Region(
                number=numbers[index] if numbers and index < len(numbers) else index + 1,
                label=labels[index] if labels and index < len(labels) else None,
                start=start,
                end=end,
            )
        )
    return spans


__all__ = [
    "CHARS_PER_TOKEN",
    "Chunk",
    "Region",
    "chunk_document",
    "content_hash",
    "regions_from_pages",
]
