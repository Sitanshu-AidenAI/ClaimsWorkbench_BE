"""Which documents state a value, beyond the one it was read from.

A model reads a value out of one passage and names that passage. That answers
"where did we read this" and it is the wrong answer to "who says so" — on a claim
notification the broker's email, the completed notice form and the engineer's
report routinely print the same policy number, and an officer verifying an
extraction wants to know whether they agree.

So after every run, each value is looked for in every *other* document on the
case, and a citation is written for each document whose text actually contains
it. Two rules make that trustworthy rather than decorative:

**Verified, never claimed.** The model is not asked which other documents support
a value, and would not be believed if it were. A corroborating citation exists
only where this module found the value in that document's own stored text — the
same posture the classification service takes toward a hallucinated line of
business, and the same one `app.services.extraction.engine` takes toward a
passage label it was never shown.

**Distinctive, or not at all.** `0` injuries appears in every document that
prints a date. `matching.searchable` is the gate, and a value that does not pass
it gets its primary citation and nothing else — silence being the honest answer
where a match would prove nothing.

Nothing here opens a file. Corroboration is a scan of text already in the
database, so a thirty-eight-field run costs one normalisation pass per document
and no downloads; the page geometry a highlight needs is resolved later, once,
for the citation a reviewer actually opens.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from app.core.logging import get_logger
from app.domain.enums import DocumentSource
from app.models.extraction import ExtractedValue
from app.models.fnol import FNOLDocument, FNOLDocumentChunk
from app.services.extraction.matching import NormalisedText, variants
from app.services.intelligence.highlight import locate_in_chunk, page_for_offset

logger = get_logger(__name__)

#: Citations kept for one value, primaries included. Past a handful the stepper
#: has stopped being a list of sources and become a search result, and the
#: occurrence search is the tool for that.
MAX_CITATIONS_PER_VALUE = 6

ROLE_PRIMARY = "primary"
ROLE_CORROBORATING = "corroborating"

STRATEGY_GROUNDED = "chunk-grounded"
STRATEGY_SEARCH = "document-search"


@dataclass(frozen=True, slots=True)
class CitationDraft:
    """One citation, before it becomes a row.

    A plain value object so the builder can be tested without a database and the
    repository stays the only thing that knows about ORM rows.
    """

    document_id: uuid.UUID
    role: str
    rank: int
    strategy: str
    chunk_id: uuid.UUID | None = None
    page_number: int | None = None
    section_label: str | None = None
    quote: str | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentText:
    """One document prepared for searching, normalised once.

    Normalising is a linear pass over the document's text. Doing it per value
    would be that pass thirty-eight times per case, which is the whole reason this
    is a separate object built by `prepare` rather than work done inside the loop.
    """

    document_id: uuid.UUID
    filename: str
    raw: str
    normalised: NormalisedText
    page_offsets: list[list[int]] | None
    is_body: bool

    @classmethod
    def of(cls, document: FNOLDocument) -> DocumentText:
        raw = document.extracted_text or ""
        return cls(
            document_id=document.id,
            filename=document.filename,
            raw=raw,
            normalised=NormalisedText.of(raw),
            page_offsets=document.page_offsets,
            is_body=document.source == DocumentSource.NOTIFICATION_BODY,
        )

    @property
    def searchable(self) -> bool:
        return bool(self.normalised)


def prepare(documents: Iterable[FNOLDocument]) -> list[DocumentText]:
    """The case's documents, normalised and ordered the way citations are ranked.

    Attachments before the notification body: where both state a value, the formal
    document is the better thing to show an officer first, and it is the same
    preference the extraction prompt expresses. Then by filename, so the order is
    stable across runs — a stepper whose "source 2 of 4" is a different file on
    every request is unusable.
    """
    prepared = [DocumentText.of(document) for document in documents]
    prepared.sort(key=lambda item: (item.is_body, item.filename.lower()))
    return [item for item in prepared if item.searchable]


class CitationBuilder:
    """Turns one value and its cited passages into the citations to store."""

    def __init__(self, *, max_citations: int = MAX_CITATIONS_PER_VALUE) -> None:
        self._max = max(1, max_citations)

    def build(
        self,
        value: ExtractedValue,
        *,
        cited: Sequence[FNOLDocumentChunk] = (),
        documents: Sequence[DocumentText] = (),
    ) -> list[CitationDraft]:
        """Every citation for `value`, primaries first.

        `cited` is the passages the model named, in the order it named them. Both
        arguments may be empty: a value an officer typed has no passages and needs
        no search, and the result is then an empty list, which is what makes the
        viewer able to say "this was entered by hand" rather than showing a page
        with nothing marked on it.
        """
        if value.value_text is None:
            return []

        pages = {item.document_id: item.page_offsets for item in documents}
        drafts = self._primaries(value, cited, pages)
        seen = {draft.document_id for draft in drafts}

        found = self._corroborations(value, documents, skip=seen)
        room = max(0, self._max - len(drafts))
        if len(found) > room:
            # Said out loud rather than truncated quietly: a screen showing four
            # sources when six documents agree is understating the evidence, and
            # the only way anyone would know is this line.
            logger.info(
                "value_citations_capped",
                field_key=value.field_key,
                corroborations_found=len(found),
                kept=room,
            )
        drafts.extend(found[:room])

        return [replace(draft, rank=rank) for rank, draft in enumerate(drafts)]

    # -- The passages the model named ----------------------------------------

    def _primaries(
        self,
        value: ExtractedValue,
        cited: Sequence[FNOLDocumentChunk],
        pages: dict[uuid.UUID, list[list[int]] | None],
    ) -> list[CitationDraft]:
        drafts: list[CitationDraft] = []
        seen: set[uuid.UUID] = set()

        for chunk in cited:
            if chunk.fnol_document_id in seen or len(drafts) >= self._max:
                # Two passages of one file is one answer to "does this file state
                # it" — the unique constraint says so — and the first is the one
                # the model named first.
                continue
            seen.add(chunk.fnol_document_id)

            start, end, text = locate_in_chunk(chunk.content, chunk.char_start, value.quote)
            located = page_for_offset(pages.get(chunk.fnol_document_id), start)
            drafts.append(
                CitationDraft(
                    document_id=chunk.fnol_document_id,
                    role=ROLE_PRIMARY,
                    rank=len(drafts),
                    strategy=STRATEGY_GROUNDED,
                    chunk_id=chunk.id,
                    page_number=(located[0] + 1) if located else chunk.page_number,
                    section_label=chunk.section_label,
                    quote=text,
                    char_start=start,
                    char_end=end,
                )
            )
        return drafts

    # -- The documents that turn out to agree --------------------------------

    def _corroborations(
        self,
        value: ExtractedValue,
        documents: Sequence[DocumentText],
        *,
        skip: set[uuid.UUID],
    ) -> list[CitationDraft]:
        forms = variants(value.value_text, data_type=value.data_type, typed=value.value_json)
        if not forms:
            # Either the value is not distinctive enough to prove anything by
            # searching, or its type is one no document writes as text. Both are
            # normal and neither is worth a log line per field per run.
            return []

        drafts: list[CitationDraft] = []
        for document in documents:
            if document.document_id in skip:
                continue
            found = document.normalised.find_any(forms)
            if found is None:
                continue

            start, end = found
            located = page_for_offset(document.page_offsets, start)
            drafts.append(
                CitationDraft(
                    document_id=document.document_id,
                    role=ROLE_CORROBORATING,
                    rank=0,  # Assigned by `build`, once the full order is known.
                    strategy=STRATEGY_SEARCH,
                    page_number=(located[0] + 1) if located else None,
                    quote=_quote(document, start, end),
                    char_start=start,
                    char_end=end,
                )
            )
        return drafts


def _quote(document: DocumentText, start: int, end: int) -> str:
    """The matched text as this document writes it.

    The original slice, not the normalised one: the offsets stored beside it are
    into the original, a viewer rendering the document's text needs the document's
    own casing, and everything downstream that searches for this string —
    `resolve_pdf_rects`, the occurrence search, the client's text layer — is
    already whitespace- and case-tolerant.
    """
    return document.raw[start:end]


__all__ = [
    "MAX_CITATIONS_PER_VALUE",
    "ROLE_CORROBORATING",
    "ROLE_PRIMARY",
    "STRATEGY_GROUNDED",
    "STRATEGY_SEARCH",
    "CitationBuilder",
    "CitationDraft",
    "DocumentText",
    "prepare",
]
