"""Entry points the worker calls.

Mirrors `app/services/mail/runner.py`, and for the same reason its docstring gives:
the route and the scheduled task must run *the same code*, or a manual trigger and a
beat tick can behave differently and nobody finds out until one of them is wrong.

This module owns the session, which the services below deliberately do not. Commit
boundaries matter here more than anywhere else in the pipeline: **one commit per
document.** The eleventh attachment failing must leave the ten before it indexed, and
its own failure recorded — which is impossible inside a transaction that has just been
rolled back. That is exactly the argument `MailIntakeService` makes for committing per
message, one layer up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.enums import DocumentIndexStatus, ProcessingState
from app.repositories.chunks import DocumentChunkRepository
from app.repositories.fnol import FNOLRepository
from app.services.intelligence.embedding import get_embedding_provider
from app.services.intelligence.indexing import (
    DocumentIndexOutcome,
    DocumentIndexService,
    TransientIndexError,
)
from app.services.intelligence.vectors import get_vector_store

logger = get_logger(__name__)


@dataclass(slots=True)
class IndexRunSummary:
    """What one indexing run did, in the shape a task result wants."""

    case_reference: str | None = None
    documents: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    reused: int = 0
    chunks: int = 0
    embedded: int = 0
    errors: list[str] = field(default_factory=list)

    def record(self, outcome: DocumentIndexOutcome) -> None:
        self.documents += 1
        self.chunks += outcome.chunks
        self.embedded += outcome.embedded
        if outcome.reused:
            self.reused += 1
        if outcome.status == DocumentIndexStatus.INDEXED:
            self.indexed += 1
        elif outcome.status == DocumentIndexStatus.SKIPPED:
            self.skipped += 1
        elif outcome.status == DocumentIndexStatus.FAILED:
            self.failed += 1
            if outcome.error:
                self.errors.append(outcome.error)


def build_index_service(session: AsyncSession) -> DocumentIndexService:
    """The index service, wired the way the API wires it."""
    return DocumentIndexService(
        DocumentChunkRepository(session),
        embeddings=get_embedding_provider(),
        vectors=get_vector_store(),
    )


async def run_document_index(document_id: uuid.UUID, *, force: bool = False) -> IndexRunSummary:
    """Index one document, in its own transaction."""
    summary = IndexRunSummary()
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        cases = FNOLRepository(session)
        document = await cases.get_document(document_id)
        if document is None:
            logger.info("index_document_missing", document_id=str(document_id))
            return summary

        service = build_index_service(session)
        try:
            outcome = await service.index_document(document, force=force)
        except TransientIndexError:
            # The row already records the attempt and the error; committing that is
            # what makes the retry bounded rather than infinite.
            await session.commit()
            raise

        summary.record(outcome)
        # Queried rather than read off `document.case`: that relationship is lazy,
        # and a lazy load in an async session raises `MissingGreenlet` instead of
        # emitting a query. It only survived review because a document created in
        # the same session already has the attribute populated — which the worker,
        # loading the row fresh, never does.
        summary.case_reference = await cases.reference_of(document.fnol_case_id)
        await session.commit()

    return summary


async def run_case_index(case_id: uuid.UUID, *, force: bool = False) -> IndexRunSummary:
    """Index every document on a case, committing after each one.

    Sequential rather than concurrent. The documents on one notice are a handful, the
    embedding provider is the bottleneck and is already batched internally, and
    sequential means one document's failure and its transaction are trivially each
    other's boundary.
    """
    summary = IndexRunSummary()
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        cases = FNOLRepository(session)
        case = await cases.get(case_id)
        if case is None:
            logger.info("index_case_missing", case_id=str(case_id))
            return summary
        summary.case_reference = case.reference
        documents = list(await cases.list_documents(case_id))

    for document in documents:
        try:
            one = await run_document_index(document.id, force=force)
        except TransientIndexError as exc:
            # Recorded and moved past. The document's attempt count is what brings it
            # back; losing the rest of the batch to it would not.
            logger.info("index_document_deferred", document_id=str(document.id), error=str(exc))
            summary.documents += 1
            summary.failed += 1
            summary.errors.append(str(exc))
            continue
        summary.documents += one.documents
        summary.indexed += one.indexed
        summary.skipped += one.skipped
        summary.failed += one.failed
        summary.reused += one.reused
        summary.chunks += one.chunks
        summary.embedded += one.embedded
        summary.errors.extend(one.errors)

    return summary


async def run_case_pipeline(case_id: uuid.UUID, *, force: bool = False) -> Any:
    """Run the FNOL pipeline over one notice, outside a request.

    Built through the same assembler the API uses, so a scheduled run and an officer
    pressing "reprocess" cannot diverge. The stages are one unit of work and a
    half-processed notice is worse than an unprocessed one, so they commit once, at
    the end.

    The exception is the announcement that the run has *started*, which is committed
    before the stages begin — see `FNOLPipeline`'s `checkpoint`. It is not part of
    the same unit of work: it describes a claim that has already happened, and held
    inside the stages' transaction it only became visible once they had finished,
    which is precisely when it had stopped being true.
    """
    # Imported here rather than at module scope: `app.api.deps.services` imports this
    # package's services to assemble them, so a top-level import would be a cycle.
    from app.api.deps.services import build_pipeline
    from app.services.ai.factory import get_ai_provider

    sessionmaker = get_session_factory()
    async with sessionmaker() as session:
        cases = FNOLRepository(session)
        case = await cases.get(case_id)
        if case is None:
            logger.info("process_case_missing", case_id=str(case_id))
            return None

        pipeline = build_pipeline(
            session,
            provider=get_ai_provider(),
            embeddings=get_embedding_provider(),
            vectors=get_vector_store(),
            checkpoint=session.commit,
        )
        result = await pipeline.run(case, force=force)
        await session.commit()
        return result


async def claim_queued_cases(limit: int | None = None) -> list[uuid.UUID]:
    """The cases the mailbox left queued, claimed for processing.

    This is the seam `docs/mail-intake.md` describes and nothing previously fulfilled:
    intake stops at `processing_state = queued`, and until now nothing read that.

    `FOR UPDATE SKIP LOCKED` so two beat ticks — or two workers — never claim the same
    notice. The state is moved to `processing` inside the same transaction that
    selected it, which is what makes the claim stick.
    """
    batch = limit or settings.docint.queue_batch_size
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        cases = FNOLRepository(session)
        claimed = await cases.claim_queued(limit=batch)
        ids = [case.id for case in claimed]
        await session.commit()

    if ids:
        logger.info("queued_cases_claimed", count=len(ids))
    return ids


async def release_stale_indexing(minutes: int | None = None) -> int:
    """Return documents stuck mid-index to the queue.

    A worker killed between setting `indexing` and finishing leaves a row that nothing
    will ever pick up again, because `indexing` is not `pending`. This is the backstop
    that makes a lost worker a delay rather than a document that silently never gets
    read.
    """
    threshold = minutes or settings.docint.stale_index_minutes
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        cases = FNOLRepository(session)
        released = await cases.release_stale_indexing(
            older_than_minutes=threshold, max_attempts=settings.docint.index_max_attempts
        )
        await session.commit()

    if released:
        logger.info("stale_indexing_released", count=released, minutes=threshold)
    return released


async def release_stale_processing(minutes: int | None = None) -> int:
    """The same backstop for cases stuck in `processing`."""
    threshold = minutes or settings.docint.stale_index_minutes
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        cases = FNOLRepository(session)
        released = await cases.release_stale_processing(older_than_minutes=threshold)
        await session.commit()

    if released:
        logger.info(
            "stale_processing_released",
            count=released,
            minutes=threshold,
            state=ProcessingState.QUEUED.value,
        )
    return released


__all__ = [
    "IndexRunSummary",
    "build_index_service",
    "claim_queued_cases",
    "release_stale_indexing",
    "release_stale_processing",
    "run_case_index",
    "run_document_index",
]
