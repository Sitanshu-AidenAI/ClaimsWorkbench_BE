"""The recovery register: money the claim expects to get back.

CLAWS's recovery handling, and the fifth of the workbench's six sections to become
real. It reported `available=False` from the day the workbench was built, because
subrogation, salvage and reinsurance were concepts the product named and did not
hold — and a zero on a financial screen that means "not tracked" is worse than an
absent figure.

Nothing here commits. The route owns the transaction, and every write flushes for
the reason `ClaimInspectionService` states: sessions are built with
`autoflush=False`, so a row that has been added is invisible to the next `SELECT`
until somebody pushes it.

**A recovery never touches the reserve.** `claim_lifecycle.incurred_minor` already
excludes recovery movements and this service adds none: the reserve is what the
claim is expected to cost, and money coming back is tracked against it. Netting an
identified-but-unbanked recovery into the reserve would shrink the figure a handler
is measured against on the strength of a solicitor's opinion — and would do it at
the authority check, which is the worst possible place.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import recovery as rules
from app.domain.enums import AuditEventType, RecoveryKind, RecoveryStatus
from app.models.claim import Claim, ClaimRecovery, ClaimRecoveryEvent, ClaimRecoveryTask
from app.repositories.claim import ClaimRepository
from app.services.fnol.audit import AuditService


class ClaimRecoveryService:
    def __init__(self, claims: ClaimRepository, audit: AuditService) -> None:
        self._claims = claims
        self._audit = audit

    # -- Opening one ---------------------------------------------------------

    async def open(
        self,
        claim: Claim,
        *,
        kind: RecoveryKind,
        label: str,
        actor: str,
        expected_minor: int = 0,
        currency: str | None = None,
        prospects: float | None = None,
        position: str | None = None,
        limitation_at: datetime | None = None,
        party_name: str | None = None,
        party_role: str | None = None,
        party_carrier: str | None = None,
        party_carrier_reference: str | None = None,
        party_contact: str | None = None,
    ) -> ClaimRecovery:
        """Identify something worth pursuing.

        Opens at `identified`, which is the honest starting state: somebody has
        spotted a route back, and nobody has done anything about it yet.

        **Many per claim, unlike the inspection.** A fire can produce a subrogation
        against a contractor, salvage on the damaged stock and a reinsurance
        recovery under the treaty, all at once and all with different counterparties
        and different limitation dates. That is why there is no unique constraint
        here and one on `claim_inspections`.

        `prospects` is optional and stays null when nobody has judged it. A recovery
        assessed as hopeless and one nobody has assessed are opposite facts, and 0.0
        would state the first about every recovery on the day it was opened.
        """
        #: **Not refused on a decided claim**, deliberately, and this is the one
        #: place in the workbench where that is right: subrogation is pursued for
        #: years after a settlement is paid, and salvage is sold after the file
        #: closes. A guard here would make the register useless on exactly the
        #: claims that have recoveries.
        booking = (claim.currency or "").upper()
        money = (currency or booking or "USD").upper()
        if booking and money != booking:
            raise ValidationError(
                f"This claim is booked in {booking}, and the recovery register has to be "
                f"too. Recording {money} against it would make the register's total a "
                "figure in neither currency."
            )
        if prospects is not None and not 0.0 <= prospects <= 1.0:
            raise ValidationError(
                "Prospects are a probability between 0 and 1. Leave it out if nobody "
                "has assessed it — that is a different answer from nought."
            )

        now = datetime.now(UTC)
        recovery = self._claims.add_recovery(
            ClaimRecovery(
                claim_id=claim.id,
                kind=str(kind),
                status=str(RecoveryStatus.IDENTIFIED),
                label=label.strip(),
                prospects=prospects,
                expected_minor=max(0, expected_minor),
                recovered_minor=0,
                currency=money,
                party_name=party_name,
                party_role=party_role,
                party_carrier=party_carrier,
                party_carrier_reference=party_carrier_reference,
                party_contact=party_contact,
                position=position,
                opened_by=actor,
                opened_at=now,
                limitation_at=limitation_at,
            )
        )
        await self._claims.flush()

        self._claims.add_recovery_event(
            ClaimRecoveryEvent(
                recovery_id=recovery.id,
                occurred_at=now,
                description=f"Identified: {label.strip()}",
                actor=actor,
            )
        )
        await self._claims.flush()

        #: Opening a route contradicts a standing conclusion that there is nothing to
        #: recover, so the conclusion goes. Cleared here rather than refused: the act
        #: of opening one *is* the reversal, and making the handler retract it first
        #: would leave a register that both pursues a recovery and says there is none
        #: every time somebody forgot. The trail keeps both statements.
        if claim.no_recovery_reason is not None:
            withdrawn = claim.no_recovery_reason
            claim.no_recovery_reason = None
            await self._claims.flush()
            self._audit.claim(
                claim,
                event_type=AuditEventType.RECOVERY_RECONSIDERED,
                summary=(f"{actor} reopened recovery on this claim, withdrawing: {withdrawn}"),
                actor=actor,
                before={"no_recovery_reason": withdrawn},
                after={"no_recovery_reason": None},
            )

        self._audit.claim(
            claim,
            event_type=AuditEventType.RECOVERY_OPENED,
            summary=(
                f"{actor} opened a {str(kind).replace('_', ' ')} recovery: {label.strip()}"
                + (f", expected at {_major(expected_minor)} {money}." if expected_minor else ".")
            ),
            actor=actor,
            after={
                "kind": str(kind),
                "label": label.strip(),
                "expected_minor": expected_minor,
                "currency": money,
            },
        )
        return recovery

    async def decline(self, claim: Claim, *, reason: str, actor: str) -> Claim:
        """Record that there is nothing worth recovering on this claim.

        The answer to a question the register could only ask. An empty register meant
        both "nobody has looked" and "somebody looked and found nothing", and the
        section had to read it as the first — so a claim whose recovery had been
        considered and closed still displayed *"Recovery has not been considered
        yet"*, on every claim, forever.

        **A reason is required and is not a formality.** It is the sentence a
        supervisor reads six months later when a limitation date has passed and
        somebody asks why nothing was pursued. "No" without a because is not a
        conclusion anybody can review.

        **Refused while a live recovery exists.** A claim cannot both be pursuing
        salvage and hold that there is nothing to recover, and the register — not
        this column — is the authority on what is being pursued. A handler who has
        given up on the routes they opened writes them off first, which is what
        `progress` is for.

        Allowed on a decided claim, for the same reason `open` is: recovery outlives
        the settlement, and so does the decision not to pursue one.
        """
        cleaned = reason.strip()
        if not cleaned:
            raise ValidationError(
                "Say why there is nothing to recover. The reason is what a supervisor "
                "reads when a limitation date has passed and nothing was pursued."
            )

        live = [
            recovery
            for recovery in await self._claims.list_recoveries(claim.id)
            if not rules.is_closed(recovery.status)
        ]
        if live:
            raise ConflictError(
                f"{claim.reference} has {len(live)} recovery route(s) still open. Write "
                "them off or bank them before recording that there is nothing to recover."
            )

        before = claim.no_recovery_reason
        claim.no_recovery_reason = cleaned
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.RECOVERY_DECLINED,
            summary=f"{actor} recorded that there is nothing to recover: {cleaned}",
            actor=actor,
            before={"no_recovery_reason": before},
            after={"no_recovery_reason": cleaned},
        )
        return claim

    # -- Moving one ----------------------------------------------------------

    async def progress(
        self,
        claim: Claim,
        *,
        recovery_id: uuid.UUID,
        actor: str,
        status: RecoveryStatus | None = None,
        note: str | None = None,
        expected_minor: int | None = None,
        recovered_minor: int | None = None,
        prospects: float | None = None,
        position: str | None = None,
        limitation_at: datetime | None = None,
    ) -> ClaimRecovery:
        """Move a recovery on, bank what came back, or revise the expectation.

        One method rather than five, because to a handler these are one act: they
        have heard something and they are writing down what it means. The audit
        summary names whichever of them actually happened.

        **Banking money is cumulative, not a replacement.** `recovered_minor` is the
        running total, and a second payment is a second call with the new total —
        which is why the guard below refuses a figure that goes *down*. A recovery
        that has banked £30,000 cannot later have banked £20,000; that is a
        correction, and a correction to a money column belongs in a ledger rather
        than an overwrite. Until recovery payments are their own rows, refusing is
        the safe half of that trade.
        """
        recovery = await self._claims.get_recovery(claim.id, recovery_id)
        if recovery is None:
            raise NotFoundError(f"{claim.reference} has no recovery {recovery_id}.")

        before = {
            "status": recovery.status,
            "expected_minor": recovery.expected_minor,
            "recovered_minor": recovery.recovered_minor,
        }
        moved: list[str] = []

        if status is not None and str(status) != recovery.status:
            if not rules.can_transition(recovery.status, status):
                raise ConflictError(
                    f"A recovery that is {recovery.status.replace('_', ' ')} cannot move to "
                    f"{str(status).replace('_', ' ')}."
                )
            recovery.status = str(status)
            moved.append(f"moved it to {str(status).replace('_', ' ')}")

        if recovered_minor is not None:
            if recovered_minor < recovery.recovered_minor:
                raise ConflictError(
                    f"{_major(recovery.recovered_minor)} {recovery.currency} has already been "
                    f"recorded as recovered. A lower figure is a correction, and this column "
                    "is a running total rather than a value to overwrite."
                )
            if recovered_minor != recovery.recovered_minor:
                arrived = recovered_minor - recovery.recovered_minor
                recovery.recovered_minor = recovered_minor
                moved.append(f"banked {_major(arrived)} {recovery.currency}")

        if expected_minor is not None and expected_minor != recovery.expected_minor:
            recovery.expected_minor = max(0, expected_minor)
            moved.append(f"revised the expectation to {_major(expected_minor)} {recovery.currency}")

        if prospects is not None:
            if not 0.0 <= prospects <= 1.0:
                raise ValidationError("Prospects are a probability between 0 and 1.")
            recovery.prospects = prospects
            moved.append(f"put prospects at {rules.prospects_band(prospects)}")

        if position is not None:
            recovery.position = position
        if limitation_at is not None:
            recovery.limitation_at = limitation_at

        if not moved and not note and position is None and limitation_at is None:
            raise ValidationError(
                "Nothing was given to change. Say what moved, or write a note against it."
            )

        now = datetime.now(UTC)
        self._claims.add_recovery_event(
            ClaimRecoveryEvent(
                recovery_id=recovery.id,
                occurred_at=now,
                description=note.strip() if note else "; ".join(moved).capitalize() + ".",
                actor=actor,
            )
        )
        await self._claims.flush()

        #: `RECOVERY_RECORDED` when money actually arrived, because that is the
        #: material event a handler skims the log for; `RECOVERY_PROGRESSED` for
        #: everything else. Two event types for one method, chosen by what happened.
        banked = recovered_minor is not None and recovered_minor > before["recovered_minor"]
        self._audit.claim(
            claim,
            event_type=(
                AuditEventType.RECOVERY_RECORDED if banked else AuditEventType.RECOVERY_PROGRESSED
            ),
            summary=(
                f"{actor} {'; '.join(moved) if moved else 'noted progress'} on {recovery.label}."
            ),
            actor=actor,
            before=before,
            after={
                "status": recovery.status,
                "expected_minor": recovery.expected_minor,
                "recovered_minor": recovery.recovered_minor,
            },
        )
        return recovery

    # -- The work it needs ---------------------------------------------------

    async def add_task(
        self,
        claim: Claim,
        *,
        label: str,
        owner: str,
        actor: str,
        recovery_id: uuid.UUID | None = None,
        due_at: datetime | None = None,
    ) -> ClaimRecoveryTask:
        """Something that has to happen for a recovery to progress.

        `recovery_id` is optional and the absence is real: "obtain the police report"
        is recovery work before anybody knows which recovery it will support.
        """
        if (
            recovery_id is not None
            and await self._claims.get_recovery(claim.id, recovery_id) is None
        ):
            raise NotFoundError(f"{claim.reference} has no recovery {recovery_id}.")

        task = self._claims.add_recovery_task(
            ClaimRecoveryTask(
                claim_id=claim.id,
                recovery_id=recovery_id,
                label=label.strip(),
                owner=owner.strip(),
                due_at=due_at,
                done=False,
                raised_by=actor,
            )
        )
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.RECOVERY_TASK_SET,
            summary=f"{actor} raised a recovery task for {owner.strip()}: {label.strip()}",
            actor=actor,
            after={"label": label.strip(), "owner": owner.strip()},
        )
        return task

    async def set_task_done(
        self, claim: Claim, *, task_id: uuid.UUID, done: bool, actor: str
    ) -> ClaimRecoveryTask:
        task = await self._claims.get_recovery_task(claim.id, task_id)
        if task is None:
            raise NotFoundError(f"{claim.reference} has no recovery task {task_id}.")
        if task.done == done:
            return task

        task.done = done
        task.done_by = actor if done else None
        task.done_at = datetime.now(UTC) if done else None
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.RECOVERY_TASK_SET,
            summary=(
                f"{actor} marked '{task.label}' as " + ("done." if done else "outstanding again.")
            ),
            actor=actor,
            after={"label": task.label, "done": done},
        )
        return task


def _major(amount_minor: int) -> str:
    """Minor units as a figure a person reads. See `ClaimInspectionService._major`."""
    return f"{amount_minor / 100:,.2f}"


__all__ = ["ClaimRecoveryService"]
