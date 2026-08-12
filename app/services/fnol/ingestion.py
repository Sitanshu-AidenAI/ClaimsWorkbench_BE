"""Getting a notification into the system, whatever it arrived as.

Six channels, one normalised model. A broker's email, a portal submission, a TPA
bordereau row, a call-centre note and an internal manual entry all become the
same `FNOLCase` with the same pipeline behind it — the channel is recorded as a
fact about provenance, not as a fork in the logic.

Deduplication is a first-class concern here rather than an afterthought, because
every one of these channels redelivers: mail servers retry, brokers resend, and
API clients replay. Three keys guard it — the client's idempotency key, the
message id, and the notice's own external reference — and a repeat returns the
existing case rather than creating a second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.domain.enums import (
    EMAIL_CHANNELS,
    AuditEventType,
    FieldSource,
    FNOLChannel,
    FNOLStatus,
    ProcessingState,
)
from app.domain.normalisation import clip, parse_email, parse_phone
from app.domain.references import FNOL_PREFIX
from app.models.fnol import FNOLCase
from app.repositories.fnol import FNOLRepository
from app.repositories.reference import ReferenceRepository
from app.services.fnol.audit import AuditService
from app.utils.sanitise import strip_html

logger = get_logger(__name__)

#: Bound on the stored body. A 2MB marketing footer is not evidence.
MAX_BODY_CHARACTERS = 100_000


@dataclass(slots=True)
class IncomingAttachment:
    filename: str
    content: bytes
    content_type: str | None = None


@dataclass(slots=True)
class IncomingEmail:
    """An email as a mailbox integration would hand it over."""

    sender: str
    recipient: str
    subject: str
    body: str
    message_id: str
    received_at: datetime | None = None
    thread_id: str | None = None
    cc: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    attachments: list[IncomingAttachment] = field(default_factory=list)
    #: Broker mailboxes and insured mailboxes are routed to different addresses,
    #: so which one this is is known at ingest rather than guessed later.
    from_broker: bool = True
    body_is_html: bool = False


@dataclass(slots=True)
class IncomingNotification:
    """The normalised form every channel is converted into."""

    channel: FNOLChannel
    received_at: datetime
    body: str
    source_metadata: dict[str, Any] = field(default_factory=dict)
    reporter_name: str | None = None
    reporter_organisation: str | None = None
    reporter_role: str | None = None
    reporter_email: str | None = None
    reporter_phone: str | None = None
    external_reference: str | None = None
    source_reference: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    idempotency_key: str | None = None
    #: Values a structured channel supplied outright. Recorded with a `channel`
    #: provenance rather than an `ai` one, because nothing read them — they were
    #: given.
    supplied_fields: dict[str, Any] = field(default_factory=dict)
    attachments: list[IncomingAttachment] = field(default_factory=list)


class FNOLIngestionService:
    """Creates the case row, or hands back the one this notice already made."""

    def __init__(
        self,
        repository: FNOLRepository,
        references: ReferenceRepository,
        audit: AuditService,
    ) -> None:
        self._repository = repository
        self._references = references
        self._audit = audit

    @staticmethod
    def from_email(email: IncomingEmail) -> IncomingNotification:
        """Normalise an email envelope into the internal model."""
        body = strip_html(email.body) if email.body_is_html else email.body
        channel = FNOLChannel.BROKER_EMAIL if email.from_broker else FNOLChannel.INSURED_EMAIL

        return IncomingNotification(
            channel=channel,
            received_at=email.received_at or datetime.now(UTC),
            # The subject is prepended because brokers put the policy number and
            # the insured in it far more often than in the first line of the body.
            body=f"Subject: {email.subject}\nFrom: {email.sender}\n\n{body}"[:MAX_BODY_CHARACTERS],
            source_metadata={
                "sender": email.sender,
                "recipient": email.recipient,
                "cc": email.cc,
                "subject": email.subject,
                "message_id": email.message_id,
                "thread_id": email.thread_id,
                "headers": email.headers,
                "attachment_names": [item.filename for item in email.attachments],
            },
            reporter_email=parse_email(email.sender),
            reporter_organisation=_organisation_from_email(email.sender),
            message_id=email.message_id,
            thread_id=email.thread_id,
            attachments=email.attachments,
        )

    async def find_existing(self, notification: IncomingNotification) -> FNOLCase | None:
        """The case this notification has already produced, if any."""
        if notification.idempotency_key:
            existing = await self._repository.find_by_idempotency_key(notification.idempotency_key)
            if existing is not None:
                return existing
        if notification.message_id:
            existing = await self._repository.find_by_message_id(notification.message_id)
            if existing is not None:
                return existing
        return None

    async def ingest(
        self, notification: IncomingNotification, *, actor: str
    ) -> tuple[FNOLCase, bool]:
        """Create the case, or return `(existing, False)` for a redelivery."""
        existing = await self.find_existing(notification)
        if existing is not None:
            logger.info(
                "fnol_ingest_duplicate_delivery",
                reference=existing.reference,
                channel=notification.channel.value,
            )
            return existing, False

        reference = await self._references.next_reference(FNOL_PREFIX)
        case = FNOLCase(
            reference=reference,
            status=FNOLStatus.RECEIVED,
            channel=notification.channel.value,
            processing_state=ProcessingState.IDLE,
            received_at=notification.received_at,
            idempotency_key=notification.idempotency_key,
            message_id=notification.message_id,
            thread_id=notification.thread_id,
            external_reference=clip(notification.external_reference, 128),
            source_reference=clip(notification.source_reference, 128),
            source_metadata=notification.source_metadata,
            source_body=notification.body,
            reporter_name=clip(notification.reporter_name, 255),
            reporter_organisation=clip(notification.reporter_organisation, 255),
            reporter_role=clip(notification.reporter_role, 96),
            reporter_email=parse_email(notification.reporter_email),
            reporter_phone=parse_phone(notification.reporter_phone),
            currency="GBP",
        )
        self._apply_supplied(case, notification)

        self._repository.add(case)
        await self._repository.flush()

        self._audit.fnol(
            case,
            event_type=AuditEventType.FNOL_CREATED,
            summary=(
                f"Notification received via "
                f"{notification.channel.value.replace('_', ' ')} and logged as {reference}."
            ),
            actor=actor,
            after={"reference": reference, "channel": notification.channel.value},
            context={
                "message_id": notification.message_id,
                "attachments": len(notification.attachments),
            },
        )

        logger.info(
            "fnol_ingested",
            reference=reference,
            channel=notification.channel.value,
            attachments=len(notification.attachments),
            has_body=bool(notification.body.strip()),
        )
        return case, True

    def _apply_supplied(self, case: FNOLCase, notification: IncomingNotification) -> None:
        """Copy the values a structured channel stated outright onto the case.

        A portal or API submission is not read — it is *given*. Writing those
        values straight to the case means the extractor has less to guess at, and
        the provenance rows record them as `channel` so the review screen can show
        an officer which values nobody interpreted.
        """
        for attribute, value in notification.supplied_fields.items():
            if value is None or not hasattr(case, attribute):
                continue
            setattr(case, attribute, value)

    async def record_supplied_provenance(
        self, case: FNOLCase, notification: IncomingNotification
    ) -> None:
        """Write field rows for values the channel supplied, if any."""
        from app.services.fnol.extraction import _FIELD_META  # local: avoids a cycle

        for path, (label, section) in _FIELD_META.items():
            attribute = path.split(".")[-1]
            if attribute not in notification.supplied_fields:
                continue
            value = notification.supplied_fields[attribute]
            if value is None:
                continue
            await self._repository.upsert_field(
                case.id,
                field_path=path,
                section=section,
                label=label,
                value_text=str(value),
                confidence=1.0,
                source=FieldSource.CHANNEL,
                evidence_snippet=f"Supplied by the {case.channel.replace('_', ' ')} channel.",
            )


def _organisation_from_email(sender: str) -> str | None:
    """A best-effort broker name from the sender's domain.

    Explicitly a guess, and treated as one: it seeds the policy matcher's broker
    signal, which scores it rather than trusting it, and the extractor overwrites
    it the moment the body names the firm.
    """
    address = parse_email(sender)
    if not address:
        return None
    domain = address.rsplit("@", 1)[-1]
    label = domain.split(".")[0]
    generic = {"gmail", "outlook", "hotmail", "yahoo", "icloud", "protonmail", "mail"}
    if label in generic or len(label) < 3:
        return None
    return label.replace("-", " ").title()


#: Which channels carry an email envelope. Exposed so the API can validate that a
#: caller posting an email payload named an email channel.
EMAIL_CHANNEL_VALUES = frozenset(channel.value for channel in EMAIL_CHANNELS)
