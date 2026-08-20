"""Reads and writes for the policy library.

A separate repository from `PolicyRepository` rather than more methods on it,
because the two answer different questions over different tables. That one narrows
the **book** — structured rows — down to a candidate set for the identification
engine. This one owns the uploaded **wordings** and the passages cut from them, and
every query in here is either bounded by a document or is a search.

The write path is **replace-per-document**, exactly as `DocumentChunkRepository`'s
is and for the same reason: passage boundaries move when the chunker changes, so
passage 7 of a re-chunked wording is not an updated passage 7 — it is different text
at a different offset, and updating in place would leave an excerpt quoting prose
that is no longer where it says it is.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import PolicyIngestStatus
from app.domain.matching import normalise_reference
from app.models.policy_document import PolicyDocument, PolicyDocumentChunk
from app.services.intelligence.chunking import Chunk

#: Rows a keyword search will consider before ranking. A library larger than this is
#: a library where the vector index is the right tool, and the floor on the fused
#: score discards the tail either way.
_KEYWORD_SCAN_LIMIT = 800

#: Terms a query contributes to the `tsquery`. Generous, because a matching query is
#: built from a whole notice; capped so a pathological one cannot build a query that
#: is slow to plan.
_MAX_KEYWORD_TERMS = 48

_WORD_RE = re.compile(r"[a-z0-9]+")


class PolicyDocumentRepository:
    """Policy wording and passage storage for one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def flush(self) -> None:
        await self._session.flush()

    # -- Document reads ------------------------------------------------------

    async def get(self, document_id: uuid.UUID) -> PolicyDocument | None:
        return await self._session.get(PolicyDocument, document_id)

    async def get_by_checksum(self, checksum: str) -> PolicyDocument | None:
        """The document with these exact bytes, if the library already holds it.

        The idempotency key for the upload endpoint. Re-uploading the same file is a
        no-op that returns the existing entry rather than a second copy — which
        matters more here than on the claim side, because a second copy of a wording
        would be retrieved against, scored, and ranked as a rival candidate for the
        same contract.
        """
        statement = select(PolicyDocument).where(PolicyDocument.checksum_sha256 == checksum)
        return (await self._session.execute(statement)).scalars().first()

    async def list_documents(
        self,
        *,
        statuses: Sequence[str] | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[PolicyDocument], int]:
        """A page of the library, newest first, and the total behind it.

        Newest first because the row an administrator is looking for is almost
        always the one they just uploaded. `extracted_text` is a `Text` column on the
        row and is loaded with it — acceptable here and not on the claim side,
        because this list is a settings screen read by a handful of people rather
        than the queue every officer polls.
        """
        statement: Select[tuple[PolicyDocument]] = select(PolicyDocument)
        counter = select(func.count(PolicyDocument.id))

        if statuses:
            statement = statement.where(PolicyDocument.ingest_status.in_(list(statuses)))
            counter = counter.where(PolicyDocument.ingest_status.in_(list(statuses)))
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            clause = or_(
                PolicyDocument.filename.ilike(pattern),
                PolicyDocument.policy_number.ilike(pattern),
                PolicyDocument.insured_name.ilike(pattern),
                PolicyDocument.insurer_name.ilike(pattern),
                PolicyDocument.broker_name.ilike(pattern),
            )
            statement = statement.where(clause)
            counter = counter.where(clause)

        rows = (
            (
                await self._session.execute(
                    statement.order_by(PolicyDocument.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        total = int((await self._session.execute(counter)).scalar_one())
        return rows, total

    async def status_counts(self) -> dict[str, int]:
        """How many documents are in each ingestion state.

        One query for the whole summary strip. The Policies screen polls while an
        upload is in flight, so this is the cheapest thing that can tell it whether
        to keep polling.
        """
        statement = select(PolicyDocument.ingest_status, func.count(PolicyDocument.id)).group_by(
            PolicyDocument.ingest_status
        )
        rows = (await self._session.execute(statement)).all()
        counts = {status.value: 0 for status in PolicyIngestStatus}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    async def count(self) -> int:
        return int(
            (await self._session.execute(select(func.count(PolicyDocument.id)))).scalar_one()
        )

    async def list_matchable(self, limit: int = 500) -> Sequence[PolicyDocument]:
        """Every document whose passages exist, for the matcher's fact lookup.

        Bounded rather than unbounded: matching reads facts for the documents that
        retrieval returned, and this is the ceiling on the keyword-only path where
        there is no vector store to narrow the field first.
        """
        statement = (
            select(PolicyDocument)
            .where(
                PolicyDocument.ingest_status.in_(
                    [PolicyIngestStatus.CHUNKED.value, PolicyIngestStatus.EMBEDDED.value]
                )
            )
            .order_by(PolicyDocument.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def list_by_ids(self, ids: Sequence[uuid.UUID]) -> Sequence[PolicyDocument]:
        """Hydrate the documents behind a set of search hits. One query."""
        if not ids:
            return []
        statement = select(PolicyDocument).where(PolicyDocument.id.in_(list(ids)))
        return (await self._session.execute(statement)).scalars().all()

    async def claim_pending(self, *, limit: int) -> Sequence[PolicyDocument]:
        """Documents waiting to be ingested, claimed for one worker.

        `FOR UPDATE SKIP LOCKED` so two beat ticks divide the work rather than
        fighting over it or doing it twice — the same claim `FNOLRepository.claim_queued`
        makes, and for the same reason.
        """
        statement = (
            select(PolicyDocument)
            .where(
                PolicyDocument.ingest_status.in_(
                    [PolicyIngestStatus.PENDING.value, PolicyIngestStatus.QUEUED.value]
                )
            )
            .order_by(PolicyDocument.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._session.execute(statement)).scalars().all()
        for row in rows:
            row.ingest_status = PolicyIngestStatus.EXTRACTING
            row.ingest_started_at = datetime.now(UTC)
        return rows

    async def release_stale(self, *, older_than_minutes: int, max_attempts: int) -> int:
        """Return documents stuck mid-ingest to the queue.

        Without this a worker killed between claiming a document and finishing it
        leaves a row in `extracting` that nothing ever looks at again — a policy
        silently never ingested, in a library somebody is matching against.
        """
        cutoff = datetime.now(UTC).timestamp() - older_than_minutes * 60
        statement = select(PolicyDocument).where(
            PolicyDocument.ingest_status == PolicyIngestStatus.EXTRACTING.value
        )
        released = 0
        for row in (await self._session.execute(statement)).scalars().all():
            started = row.ingest_started_at
            if started is not None and started.timestamp() > cutoff:
                continue
            if row.ingest_attempts >= max_attempts:
                row.ingest_status = PolicyIngestStatus.FAILED
                row.ingest_error = f"Ingestion was abandoned after {row.ingest_attempts} attempts."
            else:
                row.ingest_status = PolicyIngestStatus.PENDING
            released += 1
        return released

    # -- Document writes -----------------------------------------------------

    def add(self, document: PolicyDocument) -> PolicyDocument:
        self._session.add(document)
        return document

    async def delete(self, document: PolicyDocument) -> None:
        """Remove a document and, by cascade, its passages.

        The vector points are *not* removed here — that is the caller's job, because
        it is a network call to another datastore and it must not happen inside the
        transaction that deletes the row. `PolicyLibraryService.remove` orders the
        two so a failed vector delete leaves the row in place and retryable, rather
        than leaving orphan points nothing in Postgres can name.
        """
        await self._session.delete(document)

    # -- Passage reads -------------------------------------------------------

    async def count_chunks(self) -> int:
        return int(
            (await self._session.execute(select(func.count(PolicyDocumentChunk.id)))).scalar_one()
        )

    async def count_chunks_for_document(self, document_id: uuid.UUID) -> int:
        statement = select(func.count(PolicyDocumentChunk.id)).where(
            PolicyDocumentChunk.policy_document_id == document_id
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def list_chunks_by_refs(self, refs: Sequence[str]) -> Sequence[PolicyDocumentChunk]:
        if not refs:
            return []
        statement = select(PolicyDocumentChunk).where(PolicyDocumentChunk.chunk_ref.in_(list(refs)))
        return (await self._session.execute(statement)).scalars().all()

    async def list_chunks_for_document(
        self, document_id: uuid.UUID, *, offset: int = 0, limit: int | None = None
    ) -> Sequence[PolicyDocumentChunk]:
        statement = (
            select(PolicyDocumentChunk)
            .where(PolicyDocumentChunk.policy_document_id == document_id)
            .order_by(PolicyDocumentChunk.chunk_index)
            .offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return (await self._session.execute(statement)).scalars().all()

    async def keyword_search(
        self,
        query: str,
        *,
        limit: int,
        document_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[tuple[PolicyDocumentChunk, float]]:
        """Rank the library's passages against `query` by Postgres full text.

        The half of retrieval that needs no embedding provider, and on this corpus it
        is not a consolation prize: a policy number and an insured name are rare
        tokens, and `ts_rank` over a GIN index finds them precisely where a dense
        vector finds them approximately.

        An **`OR` of the query's terms**, not `plainto_tsquery`. `AND`-ing every
        lexeme — the obvious choice — returns nothing at all for a query built from a
        whole notice, which is retrieval that looks configured and finds nothing.
        `ts_rank` is what keeps `OR` honest: a passage matching eight terms outranks
        one matching a single common word, and the fused floor discards the tail.
        """
        terms = _lexemes(query)
        if not terms:
            return []

        tsquery = " | ".join(terms)
        rank = func.ts_rank(text("content_tsv"), func.to_tsquery("english", tsquery)).label("rank")
        statement = (
            select(PolicyDocumentChunk, rank)
            .where(text("content_tsv @@ to_tsquery('english', :kw)"))
            .params(kw=tsquery)
            .order_by(rank.desc())
            .limit(min(limit, _KEYWORD_SCAN_LIMIT))
        )
        if document_ids:
            statement = statement.where(
                PolicyDocumentChunk.policy_document_id.in_(list(document_ids))
            )

        rows = (await self._session.execute(statement)).all()
        return [(row[0], float(row[1] or 0.0)) for row in rows]

    # -- Passage writes ------------------------------------------------------

    async def replace_chunks_for_document(
        self, document: PolicyDocument, chunks: Sequence[Chunk]
    ) -> list[PolicyDocumentChunk]:
        """Delete this document's passages and write the given set.

        Returns the new rows, flushed so their ids are available — the caller needs
        them to attach vector point ids and to report what it wrote.
        """
        await self._session.execute(
            delete(PolicyDocumentChunk).where(PolicyDocumentChunk.policy_document_id == document.id)
        )

        rows = [
            PolicyDocumentChunk(
                policy_document_id=document.id,
                policy_id=document.policy_id,
                chunk_index=chunk.index,
                chunk_ref=policy_chunk_ref(document.id, chunk.index),
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
        rows: Sequence[PolicyDocumentChunk],
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

        Used when no embedding provider is configured: the passages stay searchable
        by keyword, and the next ingest run rebuilds the vectors from them.
        """
        for row in await self.list_chunks_for_document(document_id):
            row.vector_point_id = None
            row.embedding_model = None
            row.embedding_dimension = None
            row.embedded_at = None

    async def link_policy(self, document: PolicyDocument, policy_id: uuid.UUID | None) -> None:
        """Point a document at a book row, and its passages with it.

        Both, in one place, because `PolicyDocumentChunk.policy_id` is denormalised
        so a filtered search does not need a join — and a denormalised column that
        only one of two writers maintains is a column that drifts.
        """
        document.policy_id = policy_id
        for row in await self.list_chunks_for_document(document.id):
            row.policy_id = policy_id


def _lexemes(query: str) -> list[str]:
    """The searchable terms in a query, safe to interpolate into a `tsquery`.

    Only `[a-z0-9]+` survives, which is what makes building the query by
    concatenation safe: `to_tsquery` has its own syntax — `&`, `|`, `!`, `:*`,
    quotes, brackets — and a notice containing any of them would otherwise either
    raise or, worse, mean something.
    """
    seen: list[str] = []
    for match in _WORD_RE.finditer(query.lower()):
        term = match.group(0)
        if len(term) > 1 and term not in seen:
            seen.append(term)
        if len(seen) >= _MAX_KEYWORD_TERMS:
            break
    return seen


def policy_chunk_ref(document_id: uuid.UUID, index: int) -> str:
    """The stable public name of a policy passage.

    The same derivation `app/repositories/chunks.py` uses for claim passages, so one
    shape names a passage in Postgres, in the vector payload, in an API response and
    in whatever quotes it back. Derived rather than stored-and-generated for exactly
    that reason.
    """
    return f"{document_id}:{index:05d}"


def normalised_reference(value: str | None) -> str:
    """Re-exported so callers do not reach into `app.domain.matching` for one helper."""
    return normalise_reference(value)


__all__ = ["PolicyDocumentRepository", "normalised_reference", "policy_chunk_ref"]
