"""Service wiring.

One place where repositories, the AI provider and the services are assembled, so
a route asks for `FNOLContextDep` and gets a fully-built object graph rather than
constructing eleven collaborators itself. It is also the seam tests override: a
test that wants a stub provider replaces `get_ai_provider_dependency`, and
everything below it is the real code path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.db import SessionDep
from app.core.config import ExtractionSettings, settings
from app.integrations.graph.client import GraphMailClient, get_mail_client
from app.repositories.audit import AuditRepository
from app.repositories.catastrophe import CatEventRepository
from app.repositories.chunks import DocumentChunkRepository
from app.repositories.claim import ClaimRepository
from app.repositories.extraction import ExtractionRunRepository, ExtractionSchemaRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.handler import HandlerRepository
from app.repositories.mail_intake import MailIntakeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.policy import PolicyRepository
from app.repositories.policy_document import PolicyDocumentRepository
from app.repositories.reference import ReferenceRepository
from app.services.ai.base import AIProvider
from app.services.ai.factory import get_ai_provider
from app.services.documents.service import DocumentProcessingService
from app.services.extraction.engine import SchemaExtractionEngine
from app.services.extraction.locate import EvidenceLocator
from app.services.fnol.adapter import FNOLWriteBackAdapter
from app.services.fnol.assessments import AssessmentServices
from app.services.fnol.audit import AuditService
from app.services.fnol.body import NotificationBodyDocumentService
from app.services.fnol.claims import ClaimCreationService
from app.services.fnol.classification import ClassificationService
from app.services.fnol.deletion import FNOLDeletionService
from app.services.fnol.evidence import FNOLEvidenceService
from app.services.fnol.exceptions import ExceptionService
from app.services.fnol.extraction import FNOLExtractionService
from app.services.fnol.identification import PolicyIdentificationService
from app.services.fnol.ingestion import FNOLIngestionService
from app.services.fnol.matching import (
    CatastropheMatchingService,
    DuplicateDetectionService,
)
from app.services.fnol.pipeline import FNOLPipeline
from app.services.fnol.service import FNOLService
from app.services.fnol.summary import FNOLSummaryService
from app.services.fnol.triage import AssignmentService, TriageService
from app.services.intelligence.embedding import EmbeddingProvider, get_embedding_provider
from app.services.intelligence.indexing import DocumentIndexService
from app.services.intelligence.retrieval import RetrievalService
from app.services.intelligence.vectors import VectorStore, get_vector_store
from app.services.mail.intake import MailIntakeService
from app.services.mail.runner import build_mail_intake_service
from app.services.notifications.service import NotificationService
from app.services.policies.ingestion import PolicyIngestionService
from app.services.policies.library import PolicyLibraryService
from app.services.policies.matching import PolicyMatchingService
from app.services.policies.retrieval import PolicyRetrievalService
from app.services.policies.vectors import PolicyVectorStore, get_policy_vector_store


def get_ai_provider_dependency() -> AIProvider | None:
    """Overridable in tests. `None` means the deterministic path."""
    return get_ai_provider()


AIProviderDep = Annotated[AIProvider | None, Depends(get_ai_provider_dependency)]


def get_embedding_provider_dependency() -> EmbeddingProvider | None:
    """Overridable in tests. `None` means passages are keyword-searchable only."""
    return get_embedding_provider()


EmbeddingProviderDep = Annotated[
    EmbeddingProvider | None, Depends(get_embedding_provider_dependency)
]


def get_vector_store_dependency() -> VectorStore | None:
    """Overridable in tests. `None` means retrieval runs on Postgres full text alone."""
    return get_vector_store()


VectorStoreDep = Annotated[VectorStore | None, Depends(get_vector_store_dependency)]


def get_policy_vector_store_dependency() -> PolicyVectorStore | None:
    """Overridable in tests. `None` means policy matching runs on Postgres full text.

    A separate dependency from `get_vector_store_dependency` because it is a
    separate collection with a separate Protocol — see
    `app/services/policies/vectors.py` for why the two are not one store.
    """
    return get_policy_vector_store()


PolicyVectorStoreDep = Annotated[
    PolicyVectorStore | None, Depends(get_policy_vector_store_dependency)
]


def get_mail_client_dependency() -> GraphMailClient:
    """The process-wide Graph client. Overridden in tests; raises when unconfigured."""
    return get_mail_client()


MailClientDep = Annotated[GraphMailClient, Depends(get_mail_client_dependency)]


@dataclass(slots=True)
class FNOLContext:
    """Everything a FNOL route needs, built once per request."""

    session: SessionDep
    cases: FNOLRepository
    claims: ClaimRepository
    policies: PolicyRepository
    cat_events: CatEventRepository
    handlers: HandlerRepository
    references: ReferenceRepository
    audit: AuditService
    fnol: FNOLService
    ingestion: FNOLIngestionService
    pipeline: FNOLPipeline
    creation: ClaimCreationService
    #: The one path that destroys a notice. Held here rather than on `FNOLService`
    #: because it reaches past the case tables into object storage, the vector
    #: index and the mailbox ledger — collaborators the case service has no other
    #: reason to know about.
    deletion: FNOLDeletionService
    #: Policy identification: the first decision on a notice, and the one the
    #: assessment and the claim both reason against. Held on the context because the
    #: identification route reads and re-runs it directly rather than through the
    #: whole pipeline — an officer correcting a candidate should not have to wait for
    #: the fraud score to be recomputed.
    identification: PolicyIdentificationService
    triage: TriageService
    assignment: AssignmentService
    documents: DocumentProcessingService
    #: Passage storage and search. Held on the context so the evidence and search
    #: routes can read passages without building the whole FNOL service graph.
    chunks: DocumentChunkRepository
    retrieval: RetrievalService
    index: DocumentIndexService
    #: The configurable-dataset layer: what to read, what was read, and how to
    #: point at where it was read from.
    schemas: ExtractionSchemaRepository
    runs: ExtractionRunRepository
    engine: SchemaExtractionEngine
    locator: EvidenceLocator
    body: NotificationBodyDocumentService
    extraction_config: ExtractionSettings

    async def commit(self) -> None:
        """Commit the request's work.

        Routes call this once, at the end. Keeping the commit at the route
        boundary is what makes a multi-service operation — ingest, process, raise
        exceptions — atomic rather than a sequence of partial writes.
        """
        await self.session.commit()


def build_intelligence(
    session: AsyncSession,
    *,
    embeddings: EmbeddingProvider | None = None,
    vectors: VectorStore | None = None,
) -> tuple[DocumentChunkRepository, RetrievalService, DocumentIndexService]:
    """Passage storage, search and indexing over one session.

    Both collaborators are `None`-able and both degrade rather than fail: no embedding
    provider means passages are written and keyword-searchable but not vectorised, and
    no vector store means retrieval runs on Postgres full text alone.
    """
    chunks = DocumentChunkRepository(session)
    return (
        chunks,
        RetrievalService(chunks, embeddings=embeddings, vectors=vectors),
        DocumentIndexService(chunks, embeddings=embeddings, vectors=vectors),
    )


def build_pipeline(
    session: AsyncSession,
    *,
    provider: AIProvider | None,
    embeddings: EmbeddingProvider | None,
    vectors: VectorStore | None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> FNOLPipeline:
    """Assemble the pipeline.

    Separated from `build_context` so the worker can run the same pipeline the route
    runs without constructing the whole request-scoped graph around it. A scheduled
    processing run and an officer pressing "reprocess" must not be able to diverge —
    the same argument `build_mail_intake_service` makes one module over.

    All three collaborators are **required** arguments even though all three accept
    `None`. Defaulting them to the process-wide factories would look kinder and would
    silently break the seam this whole module exists to provide: in this codebase
    `None` *means* something — the deterministic reader, keyword-only retrieval — so a
    test that overrides a provider to `None` must not be handed the real one back.

    `checkpoint` is optional and defaulted, unlike those three, because it is a
    property of the *caller's* transaction rather than of the pipeline: the worker
    owns its session outright and passes `session.commit`, while a route's session
    is committed at the request boundary and must not be cut in half mid-handler.
    """
    cases = FNOLRepository(session)
    claims = ClaimRepository(session)
    policies = PolicyRepository(session)
    cat_events = CatEventRepository(session)
    audit = AuditService(AuditRepository(session))
    documents = DocumentProcessingService()

    _, retrieval, index = build_intelligence(session, embeddings=embeddings, vectors=vectors)
    runs = ExtractionRunRepository(session)

    return FNOLPipeline(
        repository=cases,
        # Wired here rather than left optional at the call site, so a notice
        # processed by the beat and one reprocessed by an officer announce
        # themselves identically. The pipeline treats it as optional because the
        # unit tests that predate notifications construct it without one.
        notifications=NotificationService(NotificationRepository(session)),
        policies=policies,
        extraction=FNOLExtractionService(
            cases, provider=provider, evidence=FNOLEvidenceService(retrieval)
        ),
        classification=ClassificationService(provider=provider),
        identification=PolicyIdentificationService(
            policies, cases, DuplicateDetectionService(cases, claims)
        ),
        catastrophe=CatastropheMatchingService(cat_events),
        assessments=AssessmentServices(cases),
        summary=FNOLSummaryService(provider=provider),
        exceptions=ExceptionService(cases),
        audit=audit,
        index=index,
        schemas=ExtractionSchemaRepository(session),
        engine=SchemaExtractionEngine(runs, retrieval=retrieval, provider=provider),
        adapter=FNOLWriteBackAdapter(cases),
        body=NotificationBodyDocumentService(cases, documents),
        checkpoint=checkpoint,
    )


def build_context(
    session: SessionDep,
    provider: AIProviderDep,
    # Defaulted, unlike `build_pipeline`'s equivalents. This function is only ever used
    # as `Depends(build_context)`, and FastAPI resolves every annotated parameter
    # regardless of its default — so the default can never take effect in production.
    # It exists for direct callers, which are tests, and `None` is the right answer for
    # them: the deterministic path with no provider and no vector store.
    embeddings: EmbeddingProviderDep = None,
    vectors: VectorStoreDep = None,
) -> FNOLContext:
    cases = FNOLRepository(session)
    claims = ClaimRepository(session)
    policies = PolicyRepository(session)
    cat_events = CatEventRepository(session)
    handlers = HandlerRepository(session)
    references = ReferenceRepository(session)
    audit = AuditService(AuditRepository(session))
    documents = DocumentProcessingService()

    triage = TriageService(claims)
    assignment = AssignmentService(handlers, claims)
    identification = PolicyIdentificationService(
        policies, cases, DuplicateDetectionService(cases, claims)
    )

    chunks, retrieval, index = build_intelligence(session, embeddings=embeddings, vectors=vectors)
    pipeline = build_pipeline(session, provider=provider, embeddings=embeddings, vectors=vectors)
    runs = ExtractionRunRepository(session)

    return FNOLContext(
        session=session,
        cases=cases,
        claims=claims,
        policies=policies,
        cat_events=cat_events,
        handlers=handlers,
        references=references,
        audit=audit,
        fnol=FNOLService(cases, audit, documents=documents, cat_events=cat_events),
        ingestion=FNOLIngestionService(cases, references, audit),
        pipeline=pipeline,
        creation=ClaimCreationService(
            fnol=cases,
            claims=claims,
            references=references,
            triage=triage,
            assignment=assignment,
            audit=audit,
        ),
        deletion=FNOLDeletionService(
            cases,
            claims,
            chunks,
            runs,
            MailIntakeRepository(session),
            NotificationRepository(session),
            audit,
            documents=documents,
            vectors=vectors,
        ),
        identification=identification,
        triage=triage,
        assignment=assignment,
        documents=documents,
        chunks=chunks,
        retrieval=retrieval,
        index=index,
        schemas=ExtractionSchemaRepository(session),
        runs=runs,
        engine=SchemaExtractionEngine(runs, retrieval=retrieval, provider=provider),
        locator=EvidenceLocator(documents),
        body=NotificationBodyDocumentService(cases, documents),
        extraction_config=settings.extraction,
    )


FNOLContextDep = Annotated[FNOLContext, Depends(build_context)]


@dataclass(slots=True)
class PolicyLibraryContext:
    """What the policy library routes need.

    Separate from `FNOLContext` for the reason `MailIntakeContext` is separate, and
    more sharply: this is a settings screen and a matching endpoint. It needs the
    library, the policy book and one embedding provider, and it needs none of the
    eleven FNOL collaborators — so listing the library does not assemble the intake
    pipeline to read a table.

    `cases` is here because matching takes an FNOL reference. Only the repository,
    never the pipeline: matching a notice against the library reads the notice and
    changes nothing about it.
    """

    session: SessionDep
    documents: PolicyDocumentRepository
    policies: PolicyRepository
    cases: FNOLRepository
    library: PolicyLibraryService
    ingestion: PolicyIngestionService
    retrieval: PolicyRetrievalService
    matching: PolicyMatchingService
    #: Whether a vector store is configured, so the list endpoint can say whether the
    #: library is semantically searchable or keyword-only. A capability, not a health
    #: check: this is configuration, never a connectivity probe.
    semantic_enabled: bool

    async def commit(self) -> None:
        await self.session.commit()


def build_policy_library(
    session: AsyncSession,
    *,
    embeddings: EmbeddingProvider | None = None,
    vectors: PolicyVectorStore | None = None,
) -> tuple[
    PolicyDocumentRepository,
    PolicyLibraryService,
    PolicyIngestionService,
    PolicyRetrievalService,
    PolicyMatchingService,
]:
    """Assemble the library over one session.

    Separated from `build_policy_context` so the worker can build the same ingestion
    service the route builds — the argument `build_pipeline` makes one module over, and
    for the same reason: a scheduled ingestion and an administrator pressing
    "re-ingest" must not be able to diverge.

    Both collaborators are `None`-able and both degrade rather than fail: no embedding
    provider means wordings are chunked and keyword-searchable but not vectorised, and
    no vector store means matching runs on Postgres full text alone.
    """
    documents = PolicyDocumentRepository(session)
    policies = PolicyRepository(session)
    retrieval = PolicyRetrievalService(documents, embeddings=embeddings, vectors=vectors)
    return (
        documents,
        PolicyLibraryService(documents, DocumentProcessingService(), vectors=vectors),
        PolicyIngestionService(
            documents, policies=policies, embeddings=embeddings, vectors=vectors
        ),
        retrieval,
        PolicyMatchingService(documents, retrieval),
    )


def build_policy_context(
    session: SessionDep,
    # Defaulted for the same reason `build_context`'s equivalents are: this function
    # is only ever used as `Depends(...)`, and FastAPI resolves every annotated
    # parameter regardless of its default, so the default can only take effect for a
    # direct caller — which is a test, and `None` is the right answer for one.
    embeddings: EmbeddingProviderDep = None,
    vectors: PolicyVectorStoreDep = None,
) -> PolicyLibraryContext:
    documents, library, ingestion, retrieval, matching = build_policy_library(
        session, embeddings=embeddings, vectors=vectors
    )
    return PolicyLibraryContext(
        session=session,
        documents=documents,
        policies=PolicyRepository(session),
        cases=FNOLRepository(session),
        library=library,
        ingestion=ingestion,
        retrieval=retrieval,
        matching=matching,
        semantic_enabled=embeddings is not None and vectors is not None,
    )


PolicyLibraryContextDep = Annotated[PolicyLibraryContext, Depends(build_policy_context)]


@dataclass(slots=True)
class MailIntakeContext:
    """What the mailbox routes need.

    Separate from `FNOLContext` because it is a different job with a different
    dependency: intake needs Microsoft Graph and does not need the AI provider,
    the policy book or the claim repositories. Building it separately means a
    request to `/mail-intake/messages` does not construct eleven FNOL
    collaborators to read a list.
    """

    session: SessionDep
    messages: MailIntakeRepository
    intake: MailIntakeService


@dataclass(slots=True)
class NotificationContext:
    """What the notification panel needs — one repository and the session.

    Separate from `FNOLContext` for the same reason `MailIntakeContext` is, and
    more sharply: this endpoint is *polled*. Building the FNOL object graph every
    few seconds to read a list would make the cheapest request in the product one
    of the most expensive.
    """

    session: SessionDep
    notifications: NotificationRepository


def build_notification_context(session: SessionDep) -> NotificationContext:
    return NotificationContext(session=session, notifications=NotificationRepository(session))


NotificationContextDep = Annotated[NotificationContext, Depends(build_notification_context)]


def build_mail_intake_context(client: MailClientDep, session: SessionDep) -> MailIntakeContext:
    # The Graph client is resolved first deliberately: a deployment with no
    # mailbox credentials should answer "not configured" without having opened a
    # database session to find that out.
    return MailIntakeContext(
        session=session,
        messages=MailIntakeRepository(session),
        # The same builder the worker uses: a manual poll and a scheduled one
        # must not be able to behave differently.
        intake=build_mail_intake_service(session, client=client),
    )


MailIntakeContextDep = Annotated[MailIntakeContext, Depends(build_mail_intake_context)]
