"""Entry points the worker calls.

Mirrors `app/services/intelligence/runner.py` and `app/services/mail/runner.py`, and
for the reason both give: the route and the scheduled task must run *the same code*,
or a manual trigger and a beat tick can behave differently and nobody finds out until
one of them is wrong.

This module owns the session, which the services below deliberately do not. Commit
boundaries matter here more than anywhere else: **one commit per document.** The
eleventh wording in a bulk load failing must leave the ten before it ingested and its
own failure recorded — which is impossible inside a transaction that has just been
rolled back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.enums import PolicyIngestStatus
from app.repositories.policy import PolicyRepository
from app.repositories.policy_document import PolicyDocumentRepository
from app.services.intelligence.embedding import get_embedding_provider
from app.services.policies.ingestion import (
    IngestOutcome,
    PolicyIngestionService,
    TransientIngestError,
)
from app.services.policies.vectors import get_policy_vector_store

logger = get_logger(__name__)


@dataclass(slots=True)
class IngestRunSummary:
    """What one ingestion run did, in the shape a task result wants."""

    documents: int = 0
    ingested: int = 0
    chunked_only: int = 0
    failed: int = 0
    reused: int = 0
    chunks: int = 0
    embedded: int = 0
    linked: int = 0
    errors: list[str] = field(default_factory=list)

    def record(self, outcome: IngestOutcome) -> None:
        self.documents += 1
        self.chunks += outcome.chunks
        self.embedded += outcome.embedded
        if outcome.reused:
            self.reused += 1
        if outcome.linked_policy_id is not None:
            self.linked += 1
        if outcome.status == PolicyIngestStatus.EMBEDDED:
            self.ingested += 1
        elif outcome.status == PolicyIngestStatus.CHUNKED:
            self.chunked_only += 1
        elif outcome.status == PolicyIngestStatus.FAILED:
            self.failed += 1
            if outcome.error:
                self.errors.append(outcome.error)


def build_ingestion_service(session: AsyncSession) -> PolicyIngestionService:
    """The ingestion service, wired the way the API wires it."""
    return PolicyIngestionService(
        PolicyDocumentRepository(session),
        policies=PolicyRepository(session),
        embeddings=get_embedding_provider(),
        vectors=get_policy_vector_store(),
    )


async def run_document_ingest(document_id: uuid.UUID, *, force: bool = False) -> IngestRunSummary:
    """Ingest one policy wording, in its own transaction."""
    summary = IngestRunSummary()
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        documents = PolicyDocumentRepository(session)
        document = await documents.get(document_id)
        if document is None:
            logger.info("policy_document_missing", document_id=str(document_id))
            return summary

        service = build_ingestion_service(session)
        try:
            outcome = await service.ingest(document, force=force)
        except TransientIngestError:
            # The row already records the attempt and the error; committing that is
            # what makes the retry bounded rather than infinite.
            await session.commit()
            raise

        summary.record(outcome)
        await session.commit()

    return summary


async def run_pending_ingest(limit: int | None = None) -> IngestRunSummary:
    """Ingest the wordings waiting in the queue, committing after each one.

    Sequential rather than concurrent. A bulk load is a handful to a few hundred
    documents, the embedding provider is the bottleneck and is already batched
    internally, and sequential means one document's failure and its transaction are
    trivially each other's boundary.
    """
    batch = limit or 20
    summary = IngestRunSummary()
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        documents = PolicyDocumentRepository(session)
        claimed = [row.id for row in await documents.claim_pending(limit=batch)]
        await session.commit()

    for document_id in claimed:
        try:
            one = await run_document_ingest(document_id)
        except TransientIngestError as exc:
            # Recorded and moved past. The document's attempt count is what brings it
            # back; losing the rest of the batch to it would not.
            logger.info(
                "policy_document_ingest_deferred",
                document_id=str(document_id),
                error=str(exc),
            )
            summary.documents += 1
            summary.failed += 1
            summary.errors.append(str(exc))
            continue
        summary.documents += one.documents
        summary.ingested += one.ingested
        summary.chunked_only += one.chunked_only
        summary.failed += one.failed
        summary.reused += one.reused
        summary.chunks += one.chunks
        summary.embedded += one.embedded
        summary.linked += one.linked
        summary.errors.extend(one.errors)

    return summary


async def release_stale_ingest(minutes: int | None = None) -> int:
    """Return wordings stuck mid-ingest to the queue.

    A worker killed between claiming a document and finishing it leaves a row in
    `extracting` that nothing will ever pick up again, because `extracting` is not
    `pending`. This is the backstop that makes a lost worker a delay rather than a
    policy silently absent from a library people are matching against.
    """
    threshold = minutes or settings.policy_library.stale_ingest_minutes
    sessionmaker = get_session_factory()

    async with sessionmaker() as session:
        documents = PolicyDocumentRepository(session)
        released = await documents.release_stale(
            older_than_minutes=threshold,
            max_attempts=settings.policy_library.ingest_max_attempts,
        )
        await session.commit()

    if released:
        logger.info("policy_stale_ingest_released", count=released, minutes=threshold)
    return released


__all__ = [
    "IngestRunSummary",
    "build_ingestion_service",
    "release_stale_ingest",
    "run_document_ingest",
    "run_pending_ingest",
]
