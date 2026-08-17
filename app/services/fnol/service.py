"""The FNOL case service — everything a human does to a notice.

Edits, overrides, documents, notes, exception resolution and the board figures.
It owns two rules that appear nowhere else:

* **Every meaningful change is recorded twice** — once as the new value, and once
  as an audit event carrying what it was before, who changed it and why. An
  officer's correction to a claims record is evidence, and evidence that only
  exists as a current value is not evidence.
* **A human's value outranks a machine's, permanently.** Setting a field marks it
  `human_modified`, which is what stops the next pipeline run from quietly
  putting the model's reading back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import FNOLSettings, settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domain import normalisation
from app.domain.enums import (
    AuditEventType,
    DuplicateResolution,
    ExceptionCode,
    ExceptionStatus,
    FieldSource,
    FNOLStatus,
    ProcessingState,
    Severity,
)
from app.domain.lifecycle import can_transition, derive_status
from app.models.fnol import FNOLCase, FNOLDocument
from app.repositories.catastrophe import CatEventRepository
from app.repositories.fnol import FNOLRepository
from app.services.documents.service import DocumentProcessingService
from app.services.fnol.audit import AuditService
from app.services.fnol.extraction import _FIELD_META
from app.services.intelligence.indexing import extraction_signature

logger = get_logger(__name__)

#: Fields an officer may set directly, mapped to their coercion.
_EDITABLE: dict[str, tuple[str, str]] = {
    path: (attribute, _kind)
    for path, attribute, _kind in (
        ("notification.reporter_name", "reporter_name", "text"),
        ("notification.reporter_organisation", "reporter_organisation", "text"),
        ("notification.reporter_role", "reporter_role", "text"),
        ("notification.reporter_email", "reporter_email", "email"),
        ("notification.reporter_phone", "reporter_phone", "phone"),
        ("policy.policy_number", "policy_number", "text"),
        ("policy.insured_name", "insured_name", "text"),
        ("policy.insured_organisation", "insured_organisation", "text"),
        ("policy.policy_type", "policy_type", "text"),
        ("loss.date_of_loss", "date_of_loss", "datetime"),
        ("loss.loss_location", "loss_location", "text"),
        ("loss.loss_country", "loss_country", "text"),
        ("loss.loss_description", "loss_description", "text"),
        ("loss.cause_of_loss", "cause_of_loss", "text"),
        ("loss.affected_assets", "affected_assets", "text"),
        ("loss.injuries", "injuries", "count"),
        ("loss.fatalities", "fatalities", "count"),
        ("financial.estimated_loss", "estimated_loss_minor", "money"),
        ("financial.repair_estimate", "repair_estimate_minor", "money"),
        ("additional.police_reference", "police_reference", "text"),
        ("additional.incident_reference", "incident_reference", "text"),
        ("additional.authorities_involved", "authorities_involved", "text"),
    )
}

#: Boolean facts an officer toggles rather than types.
_EDITABLE_FLAGS = {
    "loss.business_interruption": "business_interruption",
    "loss.structural_damage": "structural_damage",
    "loss.environmental_exposure": "environmental_exposure",
    "additional.potential_litigation": "potential_litigation",
}

_TEXT_LIMITS = {
    "loss_description": 4000,
    "loss_location": 2000,
    "affected_assets": 2000,
    "authorities_involved": 2000,
}


@dataclass(slots=True)
class BoardMetrics:
    received_today: int
    needs_review: int
    high_severity: int
    exceptions_open: int
    status_counts: dict[str, int]
    channel_counts: dict[str, int]
    severity_counts: dict[str, int]
    exception_counts: dict[str, int]


class FNOLService:
    def __init__(
        self,
        repository: FNOLRepository,
        audit: AuditService,
        *,
        documents: DocumentProcessingService | None = None,
        cat_events: CatEventRepository | None = None,
        config: FNOLSettings | None = None,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._documents = documents
        self._cat_events = cat_events
        self._config = config or settings.fnol

    # -- Reads ---------------------------------------------------------------

    async def get(self, reference: str) -> FNOLCase:
        case = await self._repository.get_by_reference(reference)
        if case is None:
            raise NotFoundError(f"No FNOL found for {reference}.")
        return case

    async def board_metrics(self) -> BoardMetrics:
        """The figures the intake command centre states across its top."""
        since = datetime.now(UTC) - timedelta(days=1)
        status_counts = await self._repository.status_counts()
        severity_counts = await self._repository.severity_counts()

        needs_review = sum(
            count
            for status, count in status_counts.items()
            if status
            in {
                FNOLStatus.NEEDS_REVIEW,
                FNOLStatus.INCOMPLETE,
                FNOLStatus.POSSIBLE_DUPLICATE,
                FNOLStatus.POLICY_MATCH_REQUIRED,
                FNOLStatus.AI_PROCESSING_FAILED,
                FNOLStatus.REFERRED,
            }
        )

        return BoardMetrics(
            received_today=await self._repository.count_received_since(since),
            needs_review=needs_review,
            high_severity=severity_counts.get(Severity.HIGH, 0)
            + severity_counts.get(Severity.CRITICAL, 0),
            exceptions_open=await self._repository.count_open_exceptions(),
            status_counts=status_counts,
            channel_counts=await self._repository.channel_counts(),
            severity_counts=severity_counts,
            exception_counts=await self._repository.count_open_exceptions_by_code(),
        )

    # -- Field edits ---------------------------------------------------------

    async def update_fields(
        self,
        case: FNOLCase,
        updates: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> list[str]:
        """Apply an officer's corrections, recording each one.

        Returns the paths that actually changed, so the caller knows whether the
        case is worth reprocessing. A field set to the value it already held is
        not a change and does not produce an audit event — an audit trail full of
        no-ops is an audit trail nobody reads.
        """
        changed: list[str] = []

        for path, raw in updates.items():
            if path in _EDITABLE_FLAGS:
                changed.extend(await self._set_flag(case, path, raw, actor=actor, reason=reason))
                continue

            if path not in _EDITABLE:
                raise ValidationError(
                    f"`{path}` is not a field that can be edited on a notification.",
                    details={"field": path},
                )

            attribute, kind = _EDITABLE[path]
            parsed, display = _coerce(kind, raw, attribute)
            previous = getattr(case, attribute, None)

            if parsed == previous:
                continue

            setattr(case, attribute, parsed)
            row = await self._repository.get_field(case.id, path)
            label, section = _FIELD_META.get(
                path, (attribute.replace("_", " ").title(), path.split(".")[0])
            )

            if row is None:
                row = await self._repository.upsert_field(
                    case.id,
                    field_path=path,
                    section=section,
                    label=label,
                    value_text=display,
                    confidence=1.0,
                    source=FieldSource.HUMAN,
                )
            if not row.human_modified:
                # Kept once, at the first correction: the value the machine read.
                row.original_value = row.value_text
            row.value_text = display
            row.confidence = 1.0
            row.source = FieldSource.HUMAN
            row.human_modified = True
            row.modified_by = actor
            row.modified_at = datetime.now(UTC)
            row.override_reason = reason

            self._audit.fnol(
                case,
                event_type=AuditEventType.FIELD_CHANGED,
                summary=f"{label} corrected by {actor}.",
                actor=actor,
                before={path: _display(previous)},
                after={path: display},
                context={"reason": reason} if reason else {},
            )
            changed.append(path)

        if changed:
            logger.info(
                "fnol_fields_updated", reference=case.reference, actor=actor, fields=changed
            )
        return changed

    async def _set_flag(
        self, case: FNOLCase, path: str, raw: Any, *, actor: str, reason: str | None
    ) -> list[str]:
        attribute = _EDITABLE_FLAGS[path]
        value = raw if isinstance(raw, bool) else normalisation.parse_bool(str(raw))
        if value is None:
            raise ValidationError(f"`{path}` must be true or false.", details={"field": path})

        previous = bool(getattr(case, attribute, False))
        if previous == value:
            return []

        setattr(case, attribute, value)
        self._audit.fnol(
            case,
            event_type=AuditEventType.FIELD_CHANGED,
            summary=f"{attribute.replace('_', ' ').title()} set to {value} by {actor}.",
            actor=actor,
            before={path: previous},
            after={path: value},
            context={"reason": reason} if reason else {},
        )
        return [path]

    # -- Overrides -----------------------------------------------------------

    async def override_severity(
        self, case: FNOLCase, *, severity: Severity, reason: str, actor: str
    ) -> None:
        previous = case.severity
        case.severity = severity.value
        case.severity_overridden = True
        case.severity_confidence = 1.0
        self._audit.fnol(
            case,
            event_type=AuditEventType.SEVERITY_OVERRIDDEN,
            summary=f"Severity set to {severity.value} by {actor}.",
            actor=actor,
            before={"severity": previous},
            after={"severity": severity.value},
            context={"reason": reason},
        )

    async def override_classification(
        self,
        case: FNOLCase,
        *,
        line_of_business: str,
        loss_type: str | None,
        claim_type: str | None,
        reason: str,
        actor: str,
    ) -> None:
        before = {
            "line_of_business": case.line_of_business,
            "loss_type": case.loss_type,
            "claim_type": case.claim_type,
        }
        case.line_of_business = line_of_business
        case.loss_type = loss_type
        case.claim_type = claim_type
        case.classification_overridden = True
        case.classification_confidence = 1.0
        self._audit.fnol(
            case,
            event_type=AuditEventType.CLASSIFICATION_OVERRIDDEN,
            summary=f"Classification set to {line_of_business} by {actor}.",
            actor=actor,
            before=before,
            after={
                "line_of_business": line_of_business,
                "loss_type": loss_type,
                "claim_type": claim_type,
            },
            context={"reason": reason},
        )

    async def confirm_cat_match(
        self, case: FNOLCase, *, confirmed: bool, actor: str, event_id: uuid.UUID | None = None
    ) -> None:
        """Accept or remove a catastrophe attribution.

        The event has to exist. A confirmation naming an id that is not a CAT
        event is refused rather than stored — a claim attributed to nothing is
        worse than a claim attributed to nothing *yet*.
        """
        if confirmed:
            target = event_id or case.cat_event_id
            if target is None:
                raise ValidationError("There is no catastrophe event to confirm.")
            if self._cat_events is not None:
                event = await self._cat_events.get(target)
                if event is None:
                    raise ValidationError("That catastrophe event does not exist.")
            case.cat_event_id = target
            case.cat_confirmed = True
            self._audit.fnol(
                case,
                event_type=AuditEventType.CAT_MATCH_CONFIRMED,
                summary=f"Catastrophe attribution confirmed by {actor}.",
                actor=actor,
                after={"cat_event_id": str(target)},
            )
        else:
            previous = case.cat_event_id
            case.cat_event_id = None
            case.cat_confirmed = False
            case.cat_confidence = None
            self._audit.fnol(
                case,
                event_type=AuditEventType.CAT_MATCH_REMOVED,
                summary=f"Catastrophe attribution removed by {actor}.",
                actor=actor,
                before={"cat_event_id": str(previous) if previous else None},
            )

    # -- Duplicates ----------------------------------------------------------

    async def resolve_duplicate(
        self,
        case: FNOLCase,
        duplicate_id: uuid.UUID,
        *,
        resolution: DuplicateResolution,
        note: str | None,
        actor: str,
    ) -> Any:
        candidate = await self._repository.get_duplicate(duplicate_id)
        if candidate is None or candidate.fnol_case_id != case.id:
            raise NotFoundError("That duplicate candidate is not on this notification.")

        candidate.resolution = resolution.value
        candidate.resolved_by = actor
        candidate.resolved_at = datetime.now(UTC)
        candidate.resolution_note = note

        if resolution is DuplicateResolution.DUPLICATE:
            # Marked a duplicate is a decision to stop working the notice, not a
            # deletion: the record stays, with the reason and the person on it.
            case.status = FNOLStatus.REJECTED
        else:
            # The candidate is decided, so the exception it raised no longer
            # applies — but only once *every* candidate has been dealt with.
            # Clearing it while another is outstanding would let a notice through
            # on the strength of the one match the officer happened to open.
            outstanding = [
                other
                for other in await self._repository.list_duplicates(case.id)
                if other.id != candidate.id and other.resolution == DuplicateResolution.UNRESOLVED
            ]
            if not outstanding:
                await self._repository.clear_exception(
                    case.id, ExceptionCode.POSSIBLE_DUPLICATE.value
                )

        self._audit.fnol(
            case,
            event_type=AuditEventType.DUPLICATE_RESOLVED,
            summary=(
                f"Possible duplicate of {candidate.candidate_reference} resolved as "
                f"“{resolution.value.replace('_', ' ')}” by {actor}."
            ),
            actor=actor,
            after={"resolution": resolution.value, "reference": candidate.candidate_reference},
            context={"note": note} if note else {},
        )
        return candidate

    # -- Exceptions ----------------------------------------------------------

    async def resolve_exception(
        self,
        case: FNOLCase,
        exception_id: uuid.UUID,
        *,
        status: ExceptionStatus,
        note: str | None,
        actor: str,
    ) -> Any:
        exception = await self._repository.get_exception(exception_id)
        if exception is None or exception.fnol_case_id != case.id:
            raise NotFoundError("That exception is not on this notification.")
        if exception.status != ExceptionStatus.OPEN:
            raise ConflictError("That exception has already been dealt with.")

        exception.status = status.value
        exception.resolution_note = note
        exception.resolved_by = actor
        exception.resolved_at = datetime.now(UTC)

        self._audit.fnol(
            case,
            event_type=AuditEventType.EXCEPTION_RESOLVED,
            summary=f"“{exception.title}” marked {status.value} by {actor}.",
            actor=actor,
            after={"code": exception.code, "status": status.value},
            context={"note": note} if note else {},
        )
        await self.refresh_status(case)
        return exception

    async def refresh_status(self, case: FNOLCase) -> FNOLStatus:
        """Recompute the case status from its currently open exceptions.

        Flushed first: the session runs with autoflush off, so an exception
        resolved a moment ago would still be read as open and the status would
        not move.
        """
        await self._repository.flush()
        open_codes = [
            exception.code
            for exception in await self._repository.list_exceptions(case.id, only_open=True)
        ]
        previous = FNOLStatus(case.status)
        current = derive_status(previous, open_exception_codes=open_codes)
        if current != previous:
            case.status = current.value
            self._audit.system(
                case,
                event_type=AuditEventType.FNOL_STATUS_CHANGED,
                summary=f"Status moved from {previous.value} to {current.value}.",
                context={"from": previous.value, "to": current.value},
            )
        return current

    async def transition(
        self, case: FNOLCase, target: FNOLStatus, *, actor: str, reason: str | None = None
    ) -> None:
        current = FNOLStatus(case.status)
        if not can_transition(current, target):
            raise ConflictError(
                f"A notification that is {current.value} cannot move to {target.value}."
            )
        case.status = target.value
        self._audit.fnol(
            case,
            event_type=AuditEventType.FNOL_STATUS_CHANGED,
            summary=f"Status set to {target.value} by {actor}.",
            actor=actor,
            before={"status": current.value},
            after={"status": target.value},
            context={"reason": reason} if reason else {},
        )

    # -- Notes ---------------------------------------------------------------

    async def add_note(self, case: FNOLCase, *, body: str, actor: str) -> Any:
        text = body.strip()
        if not text:
            raise ValidationError("A note cannot be empty.")
        note = self._repository.add_note(case.id, author=actor, body=text[:4000])
        self._audit.fnol(
            case,
            event_type=AuditEventType.NOTE_ADDED,
            summary=f"Note added by {actor}.",
            actor=actor,
        )
        return note

    # -- Documents -----------------------------------------------------------

    async def attach_document(
        self,
        case: FNOLCase,
        *,
        filename: str,
        content: bytes,
        content_type: str | None,
        source: str,
        actor: str,
    ) -> FNOLDocument:
        """Store an attachment and record it against the notice."""
        if self._documents is None:
            raise ConflictError("Document processing is not available.")

        count = await self._repository.count_documents(case.id)
        if count >= self._config.max_documents_per_case:
            raise ValidationError(
                f"A notification can carry at most {self._config.max_documents_per_case} documents."
            )

        stored = await self._documents.process(
            owner_reference=case.reference,
            filename=filename,
            content=content,
            max_bytes=self._config.max_document_bytes,
            declared_content_type=content_type,
        )

        existing = await self._repository.find_document_by_checksum(case.id, stored.checksum)
        if existing is not None:
            # The same bytes arriving twice is one document. Returned rather than
            # refused: a broker resending an email with the same attachment is
            # normal, and an error here would look like a failure to the officer.
            return existing

        document = FNOLDocument(
            fnol_case_id=case.id,
            filename=stored.filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.storage_key,
            checksum_sha256=stored.checksum,
            source=source,
            document_kind=stored.kind,
            extraction_status=stored.extraction_status.value,
            extracted_text=stored.text or None,
            text_characters=len(stored.text),
            page_count=stored.page_count,
            extraction_error=stored.extraction_error,
            uploaded_by=actor,
            # How the text was read, and where each page landed inside it. Neither is
            # derivable after the fact — the same content type can be read more than one
            # way — and `page_offsets` is what a citation's page number comes from, so a
            # document stored without it can never point an officer at a page.
            text_extractor=stored.extractor,
            page_offsets=stored.page_offsets or None,
        )
        document.extraction_signature = extraction_signature(document)
        self._repository.add_document(document)
        await self._repository.flush()

        self._audit.fnol(
            case,
            event_type=AuditEventType.DOCUMENT_UPLOADED,
            summary=f"{stored.filename} attached by {actor}.",
            actor=actor,
            after={
                "filename": stored.filename,
                "size_bytes": stored.size_bytes,
                "extraction": stored.extraction_status.value,
            },
        )
        logger.info(
            "fnol_document_attached",
            reference=case.reference,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
        )
        return document

    async def remove_document(self, case: FNOLCase, document_id: uuid.UUID, *, actor: str) -> None:
        document = await self._repository.get_document(document_id)
        if document is None or document.fnol_case_id != case.id:
            raise NotFoundError("That document is not on this notification.")
        if case.status == FNOLStatus.CLAIM_CREATED:
            raise ConflictError("Documents cannot be removed once the claim has been created.")

        filename = document.filename
        await self._repository.delete_document(document)
        if self._documents is not None:
            await self._documents.remove(document.storage_key)

        self._audit.fnol(
            case,
            event_type=AuditEventType.DOCUMENT_DELETED,
            summary=f"{filename} removed by {actor}.",
            actor=actor,
            before={"filename": filename},
        )

    # -- Processing ----------------------------------------------------------

    def mark_queued(self, case: FNOLCase) -> None:
        case.processing_state = ProcessingState.QUEUED
        case.processing_error = None


def _coerce(kind: str, raw: Any, attribute: str) -> tuple[Any, str | None]:
    """Parse an officer's input, refusing what the machine would refuse.

    The same normalisation the extraction path uses, deliberately: a date an
    officer types badly must fail the same way a date a model reads badly does,
    or the two paths disagree about what is on the case.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None

    text = str(raw).strip()

    if kind == "datetime":
        parsed = normalisation.parse_datetime(text)
        if parsed is None:
            raise ValidationError(
                "That is not a date this system will accept — it must be a real date, "
                "not in the future, and within the last ten years.",
                details={"field": attribute},
            )
        return parsed, parsed.isoformat()

    if kind == "money":
        parsed = normalisation.parse_money_minor(text)
        if parsed is None:
            raise ValidationError(
                "That is not an amount this system will accept — it must be a non-negative figure.",
                details={"field": attribute},
            )
        return parsed, text

    if kind == "count":
        parsed = normalisation.parse_count(text)
        if parsed is None:
            raise ValidationError(
                "That must be a whole number of people, or zero.",
                details={"field": attribute},
            )
        return parsed, str(parsed)

    if kind == "email":
        parsed = normalisation.parse_email(text)
        if parsed is None:
            raise ValidationError(
                "That is not a complete email address.", details={"field": attribute}
            )
        return parsed, parsed

    if kind == "phone":
        parsed = normalisation.parse_phone(text)
        if parsed is None:
            raise ValidationError(
                "That is not a usable telephone number.", details={"field": attribute}
            )
        return parsed, parsed

    limit = _TEXT_LIMITS.get(attribute, 255)
    value = normalisation.clip(text, limit)
    return value, value


def _display(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


#: Exposed so the API schema and the completeness engine agree on what is editable.
#: Every path here also appears in `FIELD_READERS`, which is the read side of the
#: same table — a field that can be edited but not read would be invisible to the
#: completeness engine the moment an officer changed it.
EDITABLE_PATHS = frozenset(_EDITABLE) | frozenset(_EDITABLE_FLAGS)
