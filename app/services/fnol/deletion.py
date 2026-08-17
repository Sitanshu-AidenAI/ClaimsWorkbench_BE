"""Removing a notification, and everything that was ever derived from it.

A notice is not one row. By the time an officer decides it should not exist, it
has spread into four stores: the notice and its nine child tables in Postgres,
the attachment bytes in object storage, the passage vectors in Qdrant, and the
mailbox ledger row that collected it. A delete that reaches only the first of
those leaves claim material on disk and searchable passages in an index that no
longer has a case to filter them by — which is worse than not deleting at all,
because the record that would have explained them has gone.

So this module owns the whole sweep, and three decisions shape it.

**Postgres first, then the stores that cannot roll back.** The row delete happens
inside the caller's transaction; the blobs and the vectors are removed after it
commits. The other order is tempting and wrong: object storage has no transaction
to join, so deleting bytes before the commit means a failed commit leaves a case
whose documents cannot be opened. Committing first can leave orphaned bytes if
the process dies between the two — garbage, which is recoverable and reportable,
rather than a broken record, which is neither. The keys are read out before the
delete precisely so the second phase still knows what to remove once the rows
naming them are gone.

**A converted notice is refused, not force-deleted.** `claims.fnol_case_id` is
`RESTRICT` in the schema and this refuses before touching it, so the constraint is
never the thing that reports the problem. A claim is the record the business runs
on; deleting the notice it was created from would leave it citing nothing.

**The audit trail survives.** Every other trace goes; the events do not, because
"who deleted this and when" is the one question a deletion must always be able to
answer, and an audit row that a delete could remove is not an audit row. The
events carry no FK to the case for exactly this reason, and one final
`fnol.deleted` event is written before the row goes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.domain.enums import AuditEventType
from app.models.fnol import FNOLCase
from app.models.mail_intake import MailIntakeMessage
from app.repositories.chunks import DocumentChunkRepository
from app.repositories.claim import ClaimRepository
from app.repositories.extraction import ExtractionRunRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.mail_intake import MailIntakeRepository
from app.repositories.notification import NotificationRepository
from app.services.documents.service import DocumentProcessingService
from app.services.fnol.audit import AuditService
from app.services.intelligence.vectors import VectorStore

logger = get_logger(__name__)


@dataclass(slots=True)
class CasePurgeReceipt:
    """What was removed, and what could not be.

    Returned to the caller and rendered straight into the API response. A
    destructive endpoint that answers `204 No Content` tells an officer nothing
    about whether the twelve documents they were worried about are actually gone,
    and gives an operator nothing to reconcile against when a store was down.
    """

    case_id: uuid.UUID
    reference: str
    #: `{"documents": 3, "fields": 41, …}` — one entry per table, counted before
    #: the delete because afterwards there is nothing left to count.
    records: dict[str, int] = field(default_factory=dict)
    #: Object-storage keys read out before the rows naming them were removed.
    storage_keys: tuple[str, ...] = ()
    blobs_removed: int = 0
    #: Keys the store refused to give up. The rows are already gone, so these are
    #: reported rather than retried — an operator with the list can finish the job.
    blobs_failed: tuple[str, ...] = ()
    #: Whether the vector index was swept. `False` means no store is configured,
    #: which is a normal deployment and not a failure.
    vectors_cleared: bool = False
    #: Non-fatal problems from the second phase, fit to show a human.
    warnings: list[str] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return sum(self.records.values())


class FNOLDeletionService:
    """The one path that destroys a notification."""

    def __init__(
        self,
        cases: FNOLRepository,
        claims: ClaimRepository,
        chunks: DocumentChunkRepository,
        runs: ExtractionRunRepository,
        mail: MailIntakeRepository,
        notifications: NotificationRepository,
        audit: AuditService,
        *,
        documents: DocumentProcessingService | None = None,
        vectors: VectorStore | None = None,
    ) -> None:
        self._cases = cases
        self._claims = claims
        self._chunks = chunks
        self._runs = runs
        self._mail = mail
        self._notifications = notifications
        self._audit = audit
        self._documents = documents
        self._vectors = vectors

    # -- Phase one: inside the caller's transaction ---------------------------

    async def remove(
        self, case: FNOLCase, *, actor: str, reason: str | None = None
    ) -> CasePurgeReceipt:
        """Delete the notice's rows and return what to sweep once they commit.

        Does not commit. The caller owns the transaction — which is what lets the
        refusal below, the audit event and the delete be one atomic decision — and
        must call `purge_stores` with the receipt after committing.
        """
        await self._refuse_if_converted(case)

        messages = list(await self._mail.list_for_case(case.id))
        receipt = await self._inventory(case, messages)

        # Written first, against a case that still exists, so the event carries the
        # reference and id the trail is read by.
        self._audit.fnol(
            case,
            event_type=AuditEventType.FNOL_DELETED,
            summary=(
                f"{case.reference} and everything held against it were deleted by {actor}."
                + (f" Reason: {reason}" if reason else "")
            ),
            actor=actor,
            # Enough of the notice to recognise it two years later, since nothing
            # else will be left to look it up in. Not the whole row: the point of a
            # deletion is that the material goes, and an audit event holding a
            # verbatim copy of it would be the deletion failing to happen.
            before={
                "reference": case.reference,
                "status": case.status,
                "channel": case.channel,
                "received_at": case.received_at.isoformat() if case.received_at else None,
                "policy_number": case.policy_number,
                "insured_name": case.insured_name,
                "loss_type": case.loss_type,
            },
            context={
                "reason": reason,
                "records": receipt.records,
                "storage_objects": len(receipt.storage_keys),
            },
        )

        # Before the case goes: the ledger's link is `SET NULL`, so leaving these
        # would strand a message row marked processed against a notice that no
        # longer exists — and nothing would ever collect that email again either,
        # because the ledger is what tells intake it has already been seen.
        for message in messages:
            await self._mail.delete_message(message)

        # The panel rows go too — the same argument as the ledger, one step further
        # on: "FNOL-2026-000123 processed successfully" sitting on someone's panel
        # and opening onto a 404 is a lie about what happened to that email. Their
        # per-person read rows go with them by cascade.
        await self._notifications.delete_for_case(case.id)

        await self._cases.delete_case(case)

        logger.info(
            "fnol_case_deleted",
            fnol=receipt.reference,
            actor=actor,
            records=receipt.total_records,
            objects=len(receipt.storage_keys),
        )
        return receipt

    # -- Phase two: after the commit ------------------------------------------

    async def purge_stores(self, receipt: CasePurgeReceipt) -> CasePurgeReceipt:
        """Remove the bytes and the vectors. Called after the rows are committed.

        Never raises. Every row naming this material is already gone, so a failure
        here cannot be undone by failing the request — it can only be reported, and
        an officer told "deleted, but two files could not be removed from storage"
        is better served than one shown a 502 for a case that has in fact gone.
        """
        if self._documents is not None:
            failed: list[str] = []
            for key in receipt.storage_keys:
                try:
                    await self._documents.remove(key)
                except Exception as exc:  # Reported, never re-raised: the rows have gone.
                    failed.append(key)
                    logger.warning(
                        "fnol_purge_blob_failed",
                        fnol=receipt.reference,
                        key=key,
                        error=str(exc),
                    )
            receipt.blobs_removed = len(receipt.storage_keys) - len(failed)
            receipt.blobs_failed = tuple(failed)
            if failed:
                receipt.warnings.append(
                    f"{len(failed)} stored file(s) could not be removed from object storage."
                )

        if self._vectors is not None:
            try:
                await self._vectors.delete_for_case(receipt.case_id)
                receipt.vectors_cleared = True
            except Exception as exc:  # Reported, never re-raised — see above.
                logger.warning("fnol_purge_vectors_failed", fnol=receipt.reference, error=str(exc))
                receipt.warnings.append(
                    "The search index could not be cleared. Its passages are orphaned "
                    "and will be removed on the next index maintenance run."
                )

        return receipt

    # -- Internals -------------------------------------------------------------

    async def _refuse_if_converted(self, case: FNOLCase) -> None:
        claim = await self._claims.get_by_fnol(case.id)
        if claim is not None or case.claim_id is not None:
            reference = claim.reference if claim is not None else "a claim"
            raise ConflictError(
                f"{case.reference} became {reference} and cannot be deleted. "
                "The claim is the record now; delete it there if it should not exist."
            )

    async def _inventory(
        self, case: FNOLCase, messages: list[MailIntakeMessage]
    ) -> CasePurgeReceipt:
        """Count what is about to go, and note where the bytes are.

        Both halves have to happen before the delete. The counts because the rows
        are the only place they exist, and the keys because `storage_key` lives on
        the document row — read it afterwards and the second phase has nothing to
        remove.
        """
        records = await self._cases.child_counts(case.id)
        records["chunks"] = await self._chunks.count_for_case(case.id)
        records.update(await self._runs.count_for_case(case.id))

        documents = await self._cases.list_documents(case.id)
        records["mail_messages"] = len(messages)
        records["mail_attachments"] = sum(len(message.attachments) for message in messages)
        records["notifications"] = await self._notifications.count_for_case(case.id)

        return CasePurgeReceipt(
            case_id=case.id,
            reference=case.reference,
            records=records,
            # De-duplicated and ordered: two rows can name one object — the same
            # attachment arriving twice is one blob — and deleting it twice would
            # count a removal that did not happen.
            storage_keys=tuple(dict.fromkeys(document.storage_key for document in documents)),
        )


__all__ = ["CasePurgeReceipt", "FNOLDeletionService"]
