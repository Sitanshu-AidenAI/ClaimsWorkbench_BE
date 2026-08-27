"""The special investigations case, and what the desk concluded per indicator.

The sixth and last of the workbench's sections to become real. It reported
`available=False` because there was no investigation record: `siu_status` was
derived from `claims.fraud_flag`, which gave it two of six states and made
*screening* mean nothing more than "the model was suspicious".

Two records, and the distinction between them is the point.

**The SIU case** is a person's decision to refer, and the investigation that
follows. One per claim, with a status only ever moved by somebody.

**A disposition** is one reviewer's verdict on one indicator. These fixed the defect
the tab admitted to: verdicts were held in a working copy on the client, so a
handler who worked through eight indicators lost all eight by reloading. They are
rows now, keyed on the indicator's own code.

Nothing here commits, and every write flushes — the reason
`ClaimInspectionService` gives.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import siu as rules
from app.domain.enums import (
    AuditEventType,
    FraudDisposition,
    SiuStatus,
)
from app.models.claim import Claim, ClaimFraudDisposition, ClaimSiuCase
from app.repositories.claim import ClaimRepository
from app.services.fnol.audit import AuditService


class ClaimSiuService:
    def __init__(self, claims: ClaimRepository, audit: AuditService) -> None:
        self._claims = claims
        self._audit = audit

    # -- The case ------------------------------------------------------------

    async def refer(
        self,
        claim: Claim,
        *,
        actor: str,
        reason: str,
        investigator: str | None = None,
        siu_reference: str | None = None,
    ) -> ClaimSiuCase:
        """Refer the claim to special investigations.

        **The reason is required.** A referral is an accusation of sorts against the
        insured, it will be read by somebody outside the claims desk, and a referral
        with no recorded rationale is the one write on this API that a regulator
        would ask about first.

        Opens the case at `referred` rather than `screening`: a handler who has
        written a reason has already screened it. `screening` exists for the state
        *before* that decision, which `screen` below records.

        Referring twice is a conflict rather than a second case. A further suspicion
        about the same claim is this case reopened — see `ClaimSiuCase`.
        """
        if not reason.strip():
            raise ValidationError(
                "Say why you are referring it. A referral is read outside this desk, and "
                "one with no recorded reason cannot be explained later."
            )

        existing = await self._claims.get_siu_case(claim.id)
        now = datetime.now(UTC)

        if existing is not None:
            if rules.is_with_siu(existing.status):
                raise ConflictError(
                    f"{claim.reference} is already with SIU ({existing.status.replace('_', ' ')})."
                )
            if not rules.can_transition(existing.status, SiuStatus.REFERRED):
                raise ConflictError(
                    f"An SIU case that is {existing.status.replace('_', ' ')} cannot be "
                    "referred. Reopen the investigation instead."
                )
            case = existing
            case.status = str(SiuStatus.REFERRED)
        else:
            case = self._claims.add_siu_case(
                ClaimSiuCase(
                    claim_id=claim.id,
                    status=str(SiuStatus.REFERRED),
                    recommended_actions=[],
                )
            )

        case.referred_by = actor
        case.referred_at = now
        case.referral_reason = reason.strip()
        if investigator is not None:
            case.investigator = investigator
        if siu_reference is not None:
            case.siu_reference = siu_reference
        #: Reopened cases carry a stale outcome otherwise, which would have the tab
        #: reporting a conclusion for an investigation that is running again.
        case.outcome = None
        case.closed_at = None
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.SIU_REFERRED,
            summary=f"{actor} referred the claim to SIU: {reason.strip()}",
            actor=actor,
            after={"status": case.status, "reason": reason.strip()},
        )
        return case

    async def screen(self, claim: Claim, *, actor: str, note: str | None = None) -> ClaimSiuCase:
        """Record that somebody is looking, without accusing anybody yet.

        The state the tab could never reach honestly. It matters because it is the
        difference between "the model scored this claim" and "a person is examining
        it", and only the second is a fact about the desk's work.
        """
        existing = await self._claims.get_siu_case(claim.id)
        if existing is not None:
            raise ConflictError(
                f"{claim.reference} already has an SIU case ({existing.status.replace('_', ' ')})."
            )

        case = self._claims.add_siu_case(
            ClaimSiuCase(
                claim_id=claim.id,
                status=str(SiuStatus.SCREENING),
                referral_reason=note.strip() if note else None,
                recommended_actions=[],
            )
        )
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.SIU_STATUS_CHANGED,
            summary=f"{actor} began screening the claim for fraud.",
            actor=actor,
            after={"status": str(SiuStatus.SCREENING)},
        )
        return case

    async def set_status(
        self,
        claim: Claim,
        *,
        status: SiuStatus,
        actor: str,
        investigator: str | None = None,
        outcome: str | None = None,
        siu_reference: str | None = None,
        recommended_actions: list[str] | None = None,
    ) -> ClaimSiuCase:
        """Move the investigation.

        **Closing requires an outcome and investigating requires an investigator.**
        An investigation nobody owns is not an investigation, and a case closed with
        no recorded conclusion is the state that makes the whole record worthless six
        months later when somebody asks what happened.
        """
        case = await self._claims.get_siu_case(claim.id)
        if case is None:
            raise NotFoundError(f"{claim.reference} has no SIU case. Refer it or screen it first.")
        if case.status == str(status):
            raise ConflictError(f"The SIU case is already {str(status).replace('_', ' ')}.")
        if not rules.can_transition(case.status, status):
            raise ConflictError(
                f"An SIU case that is {case.status.replace('_', ' ')} cannot move to "
                f"{str(status).replace('_', ' ')}."
            )

        named = investigator if investigator is not None else case.investigator
        if rules.requires_investigator(status) and not (named or "").strip():
            raise ValidationError(
                "Name the investigator. An investigation nobody owns is not one, and the "
                "tab has nobody to chase."
            )
        if rules.is_closed(status) and not (outcome or "").strip():
            raise ValidationError(
                "Record what the investigation concluded. A closed case with no outcome "
                "cannot answer the only question anybody asks of it later."
            )

        before = case.status
        case.status = str(status)
        if investigator is not None:
            case.investigator = investigator
        if siu_reference is not None:
            case.siu_reference = siu_reference
        if recommended_actions is not None:
            case.recommended_actions = [line for line in recommended_actions if line.strip()]
        if rules.is_closed(status):
            case.outcome = (outcome or "").strip()
            case.closed_at = datetime.now(UTC)
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.SIU_STATUS_CHANGED,
            summary=(
                f"{actor} moved the SIU case to {str(status).replace('_', ' ')}"
                + (f": {outcome.strip()}" if rules.is_closed(status) and outcome else ".")
            ),
            actor=actor,
            before={"status": before},
            after={"status": case.status, "investigator": case.investigator},
        )
        return case

    # -- Dispositions --------------------------------------------------------

    async def dispose(
        self,
        claim: Claim,
        *,
        code: str,
        disposition: FraudDisposition,
        actor: str,
        note: str | None = None,
    ) -> ClaimFraudDisposition:
        """Record what a reviewer concluded about one indicator.

        **This is the write that made dispositions survive a reload.**

        Keyed on the indicator's `code`, because the indicators are derived from the
        stored fraud analysis and have no rows of their own. Changing a verdict
        updates the one row the unique constraint allows rather than adding a second
        — and the audit trail keeps what it used to say, which is where the history
        of somebody changing their mind belongs.

        `discounted` **requires a note.** Accepting an indicator agrees with the
        machine and needs no defence; dismissing one is a person overruling a fraud
        signal, and that is the decision somebody will be asked to justify.
        """
        trimmed = code.strip()
        if not trimmed:
            raise ValidationError("Name the indicator being disposed of.")
        if disposition is FraudDisposition.DISCOUNTED and not (note or "").strip():
            raise ValidationError(
                "Say why you are discounting it. Dismissing a fraud indicator is a person "
                "overruling the assessment, and that is the decision worth explaining."
            )

        now = datetime.now(UTC)
        existing = await self._claims.get_fraud_disposition(claim.id, trimmed)
        before = {"disposition": existing.disposition, "note": existing.note} if existing else None

        if existing is not None:
            existing.disposition = str(disposition)
            existing.note = note.strip() if note else None
            existing.reviewed_by = actor
            existing.reviewed_at = now
            row = existing
        else:
            row = self._claims.add_fraud_disposition(
                ClaimFraudDisposition(
                    claim_id=claim.id,
                    code=trimmed,
                    disposition=str(disposition),
                    note=note.strip() if note else None,
                    reviewed_by=actor,
                    reviewed_at=now,
                )
            )
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.FRAUD_INDICATOR_DISPOSED,
            summary=(
                f"{actor} {disposition!s} the fraud indicator {trimmed}"
                + (f": {note.strip()}" if note else ".")
            ),
            actor=actor,
            before=before or {},
            after={"code": trimmed, "disposition": str(disposition)},
        )
        return row


__all__ = ["ClaimSiuService"]
