"""Service wiring.

One place where repositories, the AI provider and the services are assembled, so
a route asks for `FNOLContextDep` and gets a fully-built object graph rather than
constructing eleven collaborators itself. It is also the seam tests override: a
test that wants a stub provider replaces `get_ai_provider_dependency`, and
everything below it is the real code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from app.api.deps.db import SessionDep
from app.repositories.audit import AuditRepository
from app.repositories.catastrophe import CatEventRepository
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.handler import HandlerRepository
from app.repositories.policy import PolicyRepository
from app.repositories.reference import ReferenceRepository
from app.services.ai.base import AIProvider
from app.services.ai.factory import get_ai_provider
from app.services.documents.service import DocumentProcessingService
from app.services.fnol.assessments import AssessmentServices
from app.services.fnol.audit import AuditService
from app.services.fnol.claims import ClaimCreationService
from app.services.fnol.classification import ClassificationService
from app.services.fnol.exceptions import ExceptionService
from app.services.fnol.extraction import FNOLExtractionService
from app.services.fnol.ingestion import FNOLIngestionService
from app.services.fnol.matching import (
    CatastropheMatchingService,
    DuplicateDetectionService,
    PolicyMatchingService,
)
from app.services.fnol.pipeline import FNOLPipeline
from app.services.fnol.service import FNOLService
from app.services.fnol.summary import FNOLSummaryService
from app.services.fnol.triage import AssignmentService, TriageService


def get_ai_provider_dependency() -> AIProvider | None:
    """Overridable in tests. `None` means the deterministic path."""
    return get_ai_provider()


AIProviderDep = Annotated[AIProvider | None, Depends(get_ai_provider_dependency)]


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
    policy_matching: PolicyMatchingService
    triage: TriageService
    assignment: AssignmentService
    documents: DocumentProcessingService

    async def commit(self) -> None:
        """Commit the request's work.

        Routes call this once, at the end. Keeping the commit at the route
        boundary is what makes a multi-service operation — ingest, process, raise
        exceptions — atomic rather than a sequence of partial writes.
        """
        await self.session.commit()


def build_context(session: SessionDep, provider: AIProviderDep) -> FNOLContext:
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
    policy_matching = PolicyMatchingService(policies, cases)

    pipeline = FNOLPipeline(
        repository=cases,
        policies=policies,
        extraction=FNOLExtractionService(cases, provider=provider),
        classification=ClassificationService(provider=provider),
        policy_matching=policy_matching,
        duplicates=DuplicateDetectionService(cases, claims),
        catastrophe=CatastropheMatchingService(cat_events),
        assessments=AssessmentServices(cases),
        summary=FNOLSummaryService(provider=provider),
        exceptions=ExceptionService(cases),
        audit=audit,
    )

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
        policy_matching=policy_matching,
        triage=triage,
        assignment=assignment,
        documents=documents,
    )


FNOLContextDep = Annotated[FNOLContext, Depends(build_context)]
