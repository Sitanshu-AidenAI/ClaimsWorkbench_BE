"""SQLAlchemy mapped models.

Every model module must be imported here. Alembic's `env.py` imports this
package for its side effect, so a model that is not re-exported below is
invisible to `--autogenerate` and `alembic check`.

These mapped models cover reference data, administrative CRUD and reporting
aggregates. Performance-critical transactional tables are accessed through
asyncpg with hand-written SQL and are created by migrations directly.
"""

from __future__ import annotations

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.access import RoleCapability
from app.models.audit import AuditEvent
from app.models.claim import Claim, ClaimAssignment, ClaimTriage
from app.models.extraction import (
    ExtractedValue,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaField,
)
from app.models.fnol import (
    FNOLAIAnalysis,
    FNOLCase,
    FNOLDocument,
    FNOLDocumentChunk,
    FNOLDuplicateCandidate,
    FNOLException,
    FNOLExtractedField,
    FNOLNote,
    FNOLParty,
    FNOLPolicyMatch,
)
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage
from app.models.notification import Notification, NotificationRead
from app.models.reference_data import CatEvent, Handler, Policy, ReferenceSequence

__all__ = [
    "AuditEvent",
    "Base",
    "CatEvent",
    "Claim",
    "ClaimAssignment",
    "ClaimTriage",
    "ExtractedValue",
    "ExtractionRun",
    "ExtractionSchema",
    "ExtractionSchemaField",
    "FNOLAIAnalysis",
    "FNOLCase",
    "FNOLDocument",
    "FNOLDocumentChunk",
    "FNOLDuplicateCandidate",
    "FNOLException",
    "FNOLExtractedField",
    "FNOLNote",
    "FNOLParty",
    "FNOLPolicyMatch",
    "Handler",
    "MailIntakeAttachment",
    "MailIntakeMessage",
    "Notification",
    "NotificationRead",
    "Policy",
    "ReferenceSequence",
    "RoleCapability",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
]
