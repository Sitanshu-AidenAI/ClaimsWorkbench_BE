"""Writing notifications, in the words a handler reads them in.

A thin service over the repository, and worth having for the same reason
`AuditService` is: every call site would otherwise have to spell its own dedupe
key, pick its own tone and compose its own sentence, and the first one to get any
of those wrong produces a panel row nobody can act on.

Three rules hold everywhere in here.

**Emission never fails its caller.** A notification is the least important thing
in any transaction it takes part in — losing one costs a handler a badge, losing
the claim notification it was announcing costs the business a claim. So
`record()` swallows and logs, and every producer below it is safe to call from
inside a path whose real job is something else.

**Every event is idempotent.** Everything that produces a notification here runs
under a retry or on a schedule: a Celery task can be delivered twice, a mailbox
re-polled after a crash. The dedupe key is what makes the second delivery a
query instead of a second row on someone's panel, so each producer states one
deliberately and the choice is commented where it is not obvious.

**No commit.** The caller owns the transaction, which is what makes the
notification and the thing it describes atomic — a "processed successfully" row
that survived a rolled-back pipeline would be worse than no notification at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.domain.enums import NotificationKind, NotificationTone
from app.models.notification import Notification
from app.repositories.notification import NotificationRepository

logger = get_logger(__name__)

#: Bound on what is kept from an error message. Enough to name the cause without
#: putting a stack trace on a claims officer's screen.
MAX_DETAIL_CHARACTERS = 400


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text[:limit] if text else None


class NotificationService:
    def __init__(self, repository: NotificationRepository) -> None:
        self._repository = repository

    async def record(
        self,
        *,
        kind: NotificationKind,
        title: str,
        body: str,
        dedupe_key: str,
        tone: NotificationTone = NotificationTone.INFO,
        occurred_at: datetime | None = None,
        case_id: uuid.UUID | None = None,
        reference: str | None = None,
        mail_message_id: uuid.UUID | None = None,
        context: dict[str, Any] | None = None,
    ) -> Notification | None:
        """Write one notification, or nothing if this event already has one.

        Returns the row for a caller that wants it, `None` when the event was a
        duplicate *or* when writing failed — the two are deliberately not
        distinguished, because no caller should branch on either.
        """
        try:
            existing = await self._repository.find_by_dedupe_key(dedupe_key)
            if existing is not None:
                logger.debug("notification_already_recorded", kind=kind.value, key=dedupe_key)
                return None

            notification = Notification(
                kind=kind.value,
                tone=tone.value,
                title=title[:200],
                body=body,
                dedupe_key=dedupe_key[:512],
                occurred_at=occurred_at or datetime.now(UTC),
                fnol_case_id=case_id,
                fnol_reference=reference,
                mail_intake_message_id=mail_message_id,
                context=context or {},
            )
            self._repository.add(notification)
            await self._repository.flush()

            logger.info(
                "notification_recorded",
                kind=kind.value,
                reference=reference,
                tone=tone.value,
            )
            return notification
        except Exception as exc:
            # See the module docstring: a notification is never worth failing the
            # work it describes. Logged at error because a panel that has silently
            # stopped announcing arrivals is a real operational problem.
            logger.error(
                "notification_not_recorded",
                kind=kind.value,
                reference=reference,
                error=str(exc),
                exc_info=exc,
            )
            return None

    # -- The producers -------------------------------------------------------

    async def fnol_email_received(
        self,
        *,
        case_id: uuid.UUID,
        reference: str,
        mail_message_id: uuid.UUID | None,
        graph_message_id: str,
        mailbox: str,
        sender_name: str | None,
        sender_address: str | None,
        subject: str | None,
        received_at: datetime,
        attachments_stored: int,
        attachments_skipped: int,
        created: bool = True,
    ) -> Notification | None:
        """A broker's email became a notice, and the pipeline has it.

        Keyed on the Graph message id rather than the case: one email is one
        arrival, and a message that failed twice before succeeding should announce
        itself once. The case is the wrong key because a thread of three emails
        against one notice is three arrivals a handler wants to know about.

        `created` is what separates a new notification from a follow-up email on an
        existing one. Both are announced; only the first can honestly say that
        processing has started, because the second may be arriving on a notice that
        was read yesterday.
        """
        sender = (
            " ".join(part for part in (sender_name, sender_address) if part) or "an unknown sender"
        )
        return await self.record(
            kind=NotificationKind.FNOL_EMAIL_RECEIVED,
            tone=NotificationTone.INFO,
            title="New FNOL email received" if created else f"New email on {reference}",
            body=(
                "Processing has started." if created else "Filed against the existing notification."
            ),
            dedupe_key=f"{NotificationKind.FNOL_EMAIL_RECEIVED.value}:{graph_message_id}",
            # The moment the broker sent it, not the moment the poll found it.
            occurred_at=received_at,
            case_id=case_id,
            reference=reference,
            mail_message_id=mail_message_id,
            context={
                "sender_name": _clip(sender_name, 255),
                "sender_address": _clip(sender_address, 320),
                "sender": _clip(sender, 400),
                "subject": _clip(subject, 998),
                "mailbox": mailbox,
                "received_at": received_at.isoformat(),
                "attachments_stored": attachments_stored,
                "attachments_skipped": attachments_skipped,
                "status": "queued" if created else "filed",
            },
        )

    async def fnol_processing_started(
        self,
        *,
        case_id: uuid.UUID,
        reference: str,
        started_at: datetime,
        channel: str | None = None,
        documents: int = 0,
    ) -> Notification | None:
        """The pipeline picked the notice up.

        Keyed on the *run* — the case plus the instant it started — not on the
        case. Two deliveries of one Celery task carry the same instant and produce
        one row; a genuine second run after a failure carries a new one and
        deserves to say so, because the handler was told it failed the first time.
        """
        return await self.record(
            kind=NotificationKind.FNOL_PROCESSING_STARTED,
            tone=NotificationTone.INFO,
            title=f"Processing {reference}",
            body="Reading the notification and its documents.",
            dedupe_key=self.run_key(NotificationKind.FNOL_PROCESSING_STARTED, case_id, started_at),
            occurred_at=started_at,
            case_id=case_id,
            reference=reference,
            context={"status": "processing", "channel": channel, "documents": documents},
        )

    async def fnol_processing_succeeded(
        self,
        *,
        case_id: uuid.UUID,
        reference: str,
        started_at: datetime,
        status: str,
        exceptions_raised: int,
        fields_extracted: int = 0,
        documents_indexed: int = 0,
    ) -> Notification | None:
        """The notice was read and is on the queue.

        Tone is `warning` rather than `success` when something on it is blocking.
        The processing *succeeded* either way — the pipeline did its job — but a
        green tick on a notice that cannot proceed until a human matches a policy
        would tell the handler the opposite of what they need to know.
        """
        blocked = exceptions_raised > 0
        detail = (
            f" {exceptions_raised} item(s) need attention."
            if blocked
            else " Nothing is blocking it."
        )
        return await self.record(
            kind=NotificationKind.FNOL_PROCESSING_SUCCEEDED,
            tone=NotificationTone.WARNING if blocked else NotificationTone.SUCCESS,
            title=f"{reference} processed successfully",
            body=f"FNOL processed successfully and added to the intake queue.{detail}",
            dedupe_key=self.run_key(
                NotificationKind.FNOL_PROCESSING_SUCCEEDED, case_id, started_at
            ),
            case_id=case_id,
            reference=reference,
            context={
                "status": status,
                "exceptions_raised": exceptions_raised,
                "fields_extracted": fields_extracted,
                "documents_indexed": documents_indexed,
            },
        )

    async def fnol_processing_failed(
        self,
        *,
        case_id: uuid.UUID,
        reference: str,
        started_at: datetime,
        error: str | None,
    ) -> Notification | None:
        """The pipeline could not read the notice.

        `critical` rather than `warning`: this is the one outcome where the notice
        is on the desk carrying nothing, and nothing else will move it — the beat
        does not re-claim a `failed` case, an officer has to.
        """
        return await self.record(
            kind=NotificationKind.FNOL_PROCESSING_FAILED,
            tone=NotificationTone.CRITICAL,
            title=f"{reference} could not be processed",
            body="FNOL processing failed. Review required.",
            dedupe_key=self.run_key(NotificationKind.FNOL_PROCESSING_FAILED, case_id, started_at),
            case_id=case_id,
            reference=reference,
            context={"status": "failed", "error": _clip(error, MAX_DETAIL_CHARACTERS)},
        )

    # -- Keys -----------------------------------------------------------------

    async def mail_intake_unhealthy(
        self,
        *,
        state: object,
        detail: str,
        mailbox: str | None,
        last_run_at: datetime | None,
        last_run_age_seconds: float | None,
        dedupe_key: str,
    ) -> Notification | None:
        """Announce that mail is not being collected.

        The only producer here that is not about a notice, and the reason it
        belongs on the same panel is the reader: a handler waiting on a broker's
        email has no other way to learn that the mailbox stopped being read. Every
        other row on the panel announces mail that arrived; this one announces
        mail that cannot.

        `critical` rather than `warning`. Nothing else in the product silently
        drops inbound claims, and the tone is what decides whether somebody looks
        today or on Monday.
        """
        age = (
            f"{round(last_run_age_seconds / 3600, 1)} hours"
            if last_run_age_seconds and last_run_age_seconds >= 3600
            else f"{round((last_run_age_seconds or 0) / 60)} minutes"
        )
        return await self.record(
            kind=NotificationKind.MAIL_INTAKE_UNHEALTHY,
            tone=NotificationTone.CRITICAL,
            title="Mailbox intake is not collecting",
            body=detail,
            dedupe_key=dedupe_key,
            occurred_at=last_run_at,
            context={
                "state": str(state),
                "mailbox": mailbox,
                "last_run_at": last_run_at.isoformat() if last_run_at else None,
                "last_run_age": age if last_run_at else None,
            },
        )

    @staticmethod
    def run_key(kind: NotificationKind, case_id: uuid.UUID, started_at: datetime) -> str:
        """One key per pipeline run, so a re-delivered task is not a second row.

        Public and static because the pipeline's three events must agree on it,
        and a test asserting "one run produced one of each" needs to be able to
        spell it without reaching into the service.
        """
        return f"{kind.value}:{case_id}:{started_at.isoformat()}"


__all__ = ["NotificationService"]
