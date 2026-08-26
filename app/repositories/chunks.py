"""Reads and writes for document passages.

A separate repository from `FNOLRepository` rather than more methods on it, because
the access patterns are different in a way that matters: a case has tens of documents
and thousands of passages, and every query in here is either bounded by a document or
is a search. Mixing them into the repository that the review screen uses would make
it very easy to write the query that loads a case's entire corpus to render a page.

The write path is **replace-per-document**, not upsert-per-passage. Passage
boundaries move when the chunker changes, so passage 7 of a re-chunked document is
not an updated passage 7 — it is a different piece of text at a different offset, and
updating in place would leave a citation pointing at prose the model never read.
Deleting the document's passages and writing the new set is the only version of this
that cannot silently lie.

Field citations survive that replacement as `NULL` rather than as a wrong answer:
`fnol_extracted_fields.source_chunk_id` is `ON DELETE SET NULL`, so a re-index costs
the citations of anything not re-extracted afterwards — and the pipeline always
re-extracts after a re-index, because the index fingerprint feeding the extraction
fingerprint has moved.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fnol import FNOLDocument, FNOLDocumentChunk
from app.services.intelligence.chunking import Chunk

#: Rows a keyword search will consider before ranking. A case with more passages than
#: this is a case where the vector index is the right tool.
_KEYWORD_SCAN_LIMIT = 500

#: Terms a query contributes to the `tsquery`. A dataset field's description is a
#: sentence or two, so this is generous; the cap exists so a pathological one
#: cannot build a query that is slow to plan.
_MAX_KEYWORD_TERMS = 40

_WORD_RE = re.compile(r"[a-z0-9]+")


class DocumentChunkRepository:
    """Passage storage for one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def flush(self) -> None:
        await self._session.flush()

    # -- Reads ---------------------------------------------------------------

    async def get(self, chunk_id: uuid.UUID) -> FNOLDocumentChunk | None:
        return await self._session.get(FNOLDocumentChunk, chunk_id)

    async def get_by_ref(self, chunk_ref: str) -> FNOLDocumentChunk | None:
        statement = select(FNOLDocumentChunk).where(FNOLDocumentChunk.chunk_ref == chunk_ref)
        return (await self._session.execute(statement)).scalars().first()

    async def list_by_refs(self, refs: Sequence[str]) -> Sequence[FNOLDocumentChunk]:
        """Hydrate search hits. One query, whatever the vector store returned."""
        if not refs:
            return []
        statement = select(FNOLDocumentChunk).where(FNOLDocumentChunk.chunk_ref.in_(list(refs)))
        return (await self._session.execute(statement)).scalars().all()

    async def list_for_document(
        self, document_id: uuid.UUID, *, offset: int = 0, limit: int | None = None
    ) -> Sequence[FNOLDocumentChunk]:
        statement = (
            select(FNOLDocumentChunk)
            .where(FNOLDocumentChunk.fnol_document_id == document_id)
            .order_by(FNOLDocumentChunk.chunk_index)
            .offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return (await self._session.execute(statement)).scalars().all()

    async def list_for_case(
        self, case_id: uuid.UUID, *, limit: int | None = None
    ) -> Sequence[FNOLDocumentChunk]:
        """Every passage on a case, in document then chunk order.

        What the whole-corpus fallback sends when a case is too small for selecting
        among its passages to be worth the risk of dropping one.
        """
        statement = (
            select(FNOLDocumentChunk)
            .where(FNOLDocumentChunk.fnol_case_id == case_id)
            .order_by(FNOLDocumentChunk.fnol_document_id, FNOLDocumentChunk.chunk_index)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return (await self._session.execute(statement)).scalars().all()

    async def count_for_document(self, document_id: uuid.UUID) -> int:
        statement = select(func.count(FNOLDocumentChunk.id)).where(
            FNOLDocumentChunk.fnol_document_id == document_id
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def count_for_case(self, case_id: uuid.UUID) -> int:
        """How many passages a case has. What `retrieval_min_chunks` is compared to."""
        statement = select(func.count(FNOLDocumentChunk.id)).where(
            FNOLDocumentChunk.fnol_case_id == case_id
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def count_embedded_for_case(self, case_id: uuid.UUID) -> int:
        statement = select(func.count(FNOLDocumentChunk.id)).where(
            FNOLDocumentChunk.fnol_case_id == case_id,
            FNOLDocumentChunk.vector_point_id.is_not(None),
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def keyword_search(
        self, case_id: uuid.UUID, query: str, *, limit: int, document_id: uuid.UUID | None = None
    ) -> list[tuple[FNOLDocumentChunk, float]]:
        """Rank a case's passages against `query` by Postgres full text.

        This is the half of retrieval that needs no embedding provider and no vector
        store, which is what makes the feature degrade to something real rather than
        to nothing.

        The query is turned into an **`OR` of its terms**, ranked by `ts_rank`. That
        matters more than it looks. `plainto_tsquery` — the obvious choice, and what
        this used before datasets existed — `AND`s every lexeme, which is fine for a
        five-word section query like "date and time of loss" and returns *nothing at
        all* for a field description written as a sentence. Since a dataset field's
        description is deliberately long prose, an `AND` query would have made the
        keyword-only path silently return zero hits for every field on any deployment
        with no embedding provider: retrieval that looks configured and finds nothing.

        `ts_rank` is what keeps `OR` honest — a passage matching eight of the terms
        outranks one matching a single common word, and the fused score floor in
        `RetrievalService` discards the tail.
        """
        terms = _lexemes(query)
        if not terms:
            return []

        tsquery = " | ".join(terms)
        rank = func.ts_rank(text("content_tsv"), func.to_tsquery("english", tsquery)).label("rank")
        statement = (
            select(FNOLDocumentChunk, rank)
            .where(
                FNOLDocumentChunk.fnol_case_id == case_id,
                text("content_tsv @@ to_tsquery('english', :kw)"),
            )
            .params(kw=tsquery)
            .order_by(rank.desc())
            .limit(min(limit, _KEYWORD_SCAN_LIMIT))
        )
        if document_id is not None:
            statement = statement.where(FNOLDocumentChunk.fnol_document_id == document_id)

        rows = (await self._session.execute(statement)).all()
        return [(row[0], float(row[1] or 0.0)) for row in rows]

    # -- Writes --------------------------------------------------------------

    async def replace_for_document(
        self, document: FNOLDocument, chunks: Sequence[Chunk]
    ) -> list[FNOLDocumentChunk]:
        """Delete this document's passages and write the given set.

        Returns the new rows, flushed so their ids are available — the caller needs
        them to attach vector point ids, and to report what it wrote.
        """
        await self._session.execute(
            delete(FNOLDocumentChunk).where(FNOLDocumentChunk.fnol_document_id == document.id)
        )

        rows = [
            FNOLDocumentChunk(
                fnol_document_id=document.id,
                fnol_case_id=document.fnol_case_id,
                chunk_index=chunk.index,
                chunk_ref=chunk_ref(document.id, chunk.index),
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_estimate,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                page_number=chunk.page_number,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                section_label=chunk.section_label,
            )
            for chunk in chunks
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def mark_embedded(
        self,
        rows: Sequence[FNOLDocumentChunk],
        *,
        point_ids: dict[str, uuid.UUID],
        model: str,
        dimension: int,
    ) -> int:
        """Record which passages made it into the vector index."""
        stamped = datetime.now(UTC)
        count = 0
        for row in rows:
            point_id = point_ids.get(row.chunk_ref)
            if point_id is None:
                continue
            row.vector_point_id = point_id
            row.embedding_model = model
            row.embedding_dimension = dimension
            row.embedded_at = stamped
            count += 1
        return count

    async def clear_vectors_for_document(self, document_id: uuid.UUID) -> None:
        """Forget that a document's passages were ever indexed.

        Used when the vector store is emptied or a model changes: the passages stay
        searchable by keyword, and the next index run rebuilds the vectors from them.
        """
        for row in await self.list_for_document(document_id):
            row.vector_point_id = None
            row.embedding_model = None
            row.embedding_dimension = None
            row.embedded_at = None


def _lexemes(query: str) -> list[str]:
    """The searchable terms in a query, safe to interpolate into a `tsquery`.

    Only `[a-z0-9]+` survives, which is what makes building the query string by
    concatenation safe: `to_tsquery` has its own syntax — `&`, `|`, `!`, `:*`,
    quotes, brackets — and a description containing any of them would otherwise
    either raise or, worse, mean something.

    Single characters are dropped as noise, and the term count is capped: a
    400-word description would otherwise build a `tsquery` long enough to be slow
    to plan, and the fortieth term contributes nothing a rank can see.
    """
    seen: list[str] = []
    for match in _WORD_RE.finditer(query.lower()):
        term = match.group(0)
        if len(term) > 1 and term not in seen:
            seen.append(term)
        if len(seen) >= _MAX_KEYWORD_TERMS:
            break
    return seen


def chunk_ref(document_id: uuid.UUID, index: int) -> str:
    """The stable public name of a passage.

    Derived rather than stored-and-generated so that the same passage has the same
    name in Postgres, in the vector store's payload, in the extraction prompt and in
    the model's answer — four places that must agree for a citation to resolve.
    """
    return f"{document_id}:{index:05d}"


__all__ = ["DocumentChunkRepository", "chunk_ref"]
