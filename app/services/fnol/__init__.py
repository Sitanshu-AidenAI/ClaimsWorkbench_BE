"""FNOL application services.

One module per responsibility, and one orchestrator (`pipeline`) that runs them in
order. No service knows about HTTP, and the routes know about none of the rules —
which is what lets the same pipeline be driven by an API call, a mailbox poller
or a Celery task without any of them re-implementing it.
"""

from __future__ import annotations

from app.services.fnol.assessments import AssessmentServices
from app.services.fnol.audit import AuditService
from app.services.fnol.claims import ClaimCreationService
from app.services.fnol.classification import ClassificationService
from app.services.fnol.exceptions import ExceptionService
from app.services.fnol.extraction import ExtractionOutcome, FNOLExtractionService
from app.services.fnol.ingestion import FNOLIngestionService, IncomingEmail, IncomingNotification
from app.services.fnol.matching import (
    CatastropheMatchingService,
    DuplicateDetectionService,
    PolicyMatchingService,
)
from app.services.fnol.pipeline import FNOLPipeline
from app.services.fnol.service import FNOLService
from app.services.fnol.summary import FNOLSummaryService
from app.services.fnol.triage import AssignmentService, TriageService

__all__ = [
    "AssessmentServices",
    "AssignmentService",
    "AuditService",
    "CatastropheMatchingService",
    "ClaimCreationService",
    "ClassificationService",
    "DuplicateDetectionService",
    "ExceptionService",
    "ExtractionOutcome",
    "FNOLExtractionService",
    "FNOLIngestionService",
    "FNOLPipeline",
    "FNOLService",
    "FNOLSummaryService",
    "IncomingEmail",
    "IncomingNotification",
    "PolicyMatchingService",
    "TriageService",
]
