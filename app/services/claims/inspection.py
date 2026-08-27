"""The loss-adjuster visit: commissioning it, booking it, and what it found.

The tab this serves used to report its own absence. What it needed was not a
screen but a record, and this is that record's write side.

Six acts, and they follow the shape of the job rather than the shape of a CRUD
table: **commission** a visit, **book** it, note that the adjuster **attended**,
record what they **found**, raise and clear the **actions** their report leaves
behind, and **close** it. Each moves the inspection's status through
`app.domain.inspection`'s machine, so an out-of-order call is a 409 rather than a
row that makes no sense — a visit cannot be attended before it is booked, and a
report cannot arrive on a claim nobody instructed.

Nothing here commits. The route owns the transaction, for the reason
`FNOLContext.commit` gives.

**Every write flushes, and that is not optional.** The session is built with
`autoflush=False`, so a row that has been `add`ed is invisible to the next `SELECT`
until somebody pushes it. Two of these methods read the record straight back —
commissioning and booking in one request is the common case — and without the flush
the second half of that request asks the database for a row still sitting in the
session and is told, correctly and uselessly, that no inspection exists. Flushing is
not committing: the route still owns the transaction and a failure still rolls the
whole thing back. What it buys is that a constraint fires at the write that broke it
rather than at the commit three calls later.

Why this belongs to the claim and not to intake: an inspection is commissioned
*because* a claim exists and is being assessed. The notice's job finished when the
claim was created. It is the same line `app.services.claims` draws everywhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import claim_lifecycle
from app.domain import inspection as rules
from app.domain.enums import (
    AuditEventType,
    DamageSeverity,
    InspectionStatus,
)
from app.models.claim import (
    Claim,
    ClaimInspection,
    ClaimInspectionAction,
    ClaimInspectionObservation,
)
from app.repositories.claim import ClaimRepository
from app.services.fnol.audit import AuditService


class ClaimInspectionService:
    def __init__(self, claims: ClaimRepository, audit: AuditService) -> None:
        self._claims = claims
        self._audit = audit

    # -- Commissioning -------------------------------------------------------

    async def commission(
        self,
        claim: Claim,
        *,
        actor: str,
        adjuster_name: str | None = None,
        adjuster_firm: str | None = None,
        report_due_at: datetime | None = None,
        site_kind: str | None = None,
        site_address: str | None = None,
        site_identifier: str | None = None,
        site_contact_name: str | None = None,
        site_contact_phone: str | None = None,
        site_access_note: str | None = None,
    ) -> ClaimInspection:
        """Instruct a visit on this claim.

        **The site defaults to the loss location and nothing else.** An adjuster
        needs an address to attend, the claim already holds where the loss
        happened, and making the caller retype it invites a typo on the one field
        the visit cannot proceed without. Anything more specific — the unit, the
        gate code, who has the keys — is the caller's to supply, and is null until
        somebody does rather than guessed at.

        One per claim. A second call is a conflict rather than a second row: the
        `more_needed → visit_booked` loop is how a follow-up visit is recorded, and
        two concurrent inspections are out of scope by design — see
        `ClaimInspection`.

        Refused on a decided claim, for the reason a reserve movement is: a claim
        that has been settled or declined is not one you send an adjuster to.
        """
        if claim_lifecycle.is_terminal(claim.status):
            raise ConflictError(
                f"Claim {claim.reference} is {claim.status.replace('_', ' ')}. "
                "A decided claim is not one to commission a visit on."
            )

        if await self._claims.get_inspection(claim.id) is not None:
            raise ConflictError(
                f"{claim.reference} already has an inspection. Book a further visit on the "
                "existing one rather than commissioning a second."
            )

        now = datetime.now(UTC)
        inspection = self._claims.add_inspection(
            ClaimInspection(
                claim_id=claim.id,
                status=str(InspectionStatus.TO_SCHEDULE),
                adjuster_name=adjuster_name,
                adjuster_firm=adjuster_firm,
                commissioned_by=actor,
                commissioned_at=now,
                report_due_at=report_due_at,
                site_kind=site_kind,
                #: The loss location, because the visit cannot happen without one
                #: and the claim already knows it.
                site_address=site_address or claim.loss_location,
                site_identifier=site_identifier,
                site_contact_name=site_contact_name,
                site_contact_phone=site_contact_phone,
                site_access_note=site_access_note,
            )
        )
        #: Before the audit line, and before anything reads it back. Booking in the
        #: same request is the common case, and `schedule` finds the inspection with
        #: a query rather than from the object it was handed.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_COMMISSIONED,
            summary=(
                f"{actor} commissioned a field inspection"
                + (f" with {adjuster_firm}." if adjuster_firm else ".")
            ),
            actor=actor,
            after={
                "status": str(InspectionStatus.TO_SCHEDULE),
                "adjuster_firm": adjuster_firm,
                "adjuster_name": adjuster_name,
            },
        )
        return inspection

    # -- Booking and attending -----------------------------------------------

    async def schedule(
        self,
        claim: Claim,
        *,
        scheduled_at: datetime,
        actor: str,
        adjuster_name: str | None = None,
        adjuster_firm: str | None = None,
        reference: str | None = None,
        report_due_at: datetime | None = None,
    ) -> ClaimInspection:
        """Book the visit, or move it.

        Legal from `to_schedule` and from `more_needed` — the second being the
        follow-up visit, which is the normal outcome on a large loss. Booking from
        `visit_booked` is a rebooking and goes through `to_schedule` in the machine,
        which this does implicitly: the transition checked is to `visit_booked`, and
        an already-booked inspection is allowed to move its date.

        `adjuster_name`, `adjuster_firm` and `reference` are accepted here as well
        as at commissioning because that is when they usually arrive: a desk
        instructs a firm, and the named adjuster and their own reference come back
        with the acknowledgement.
        """
        inspection = await self._require(claim)

        if inspection.status == InspectionStatus.VISIT_BOOKED:
            # A rebooking. Already in the target state, so the machine has nothing
            # to say and the date simply moves — recorded as a rebooking, not a
            # first booking, so the trail keeps the difference.
            rebooked = inspection.scheduled_at
        elif rules.can_transition(inspection.status, InspectionStatus.VISIT_BOOKED):
            rebooked = None
        else:
            raise ConflictError(
                f"An inspection that is {inspection.status.replace('_', ' ')} cannot be booked."
            )

        before = {"status": inspection.status, "scheduled_at": _iso(inspection.scheduled_at)}
        inspection.status = str(InspectionStatus.VISIT_BOOKED)
        inspection.scheduled_at = scheduled_at
        if adjuster_name is not None:
            inspection.adjuster_name = adjuster_name
        if adjuster_firm is not None:
            inspection.adjuster_firm = adjuster_firm
        if reference is not None:
            inspection.reference = reference
        if report_due_at is not None:
            inspection.report_due_at = report_due_at

        # The route rebuilds the section from queries, which have to see this.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_SCHEDULED,
            summary=(
                f"{actor} moved the inspection visit to {scheduled_at:%d %b %Y}."
                if rebooked
                else f"{actor} booked the inspection visit for {scheduled_at:%d %b %Y}."
            ),
            actor=actor,
            before=before,
            after={
                "status": inspection.status,
                "scheduled_at": _iso(scheduled_at),
                "reference": inspection.reference,
            },
        )
        return inspection

    async def record_attendance(
        self,
        claim: Claim,
        *,
        attended_at: datetime,
        actor: str,
        summary: str | None = None,
        photographs: int | None = None,
        measurements: int | None = None,
        statements: int | None = None,
        documents: int | None = None,
    ) -> ClaimInspection:
        """The adjuster went. Records that, and what they came back with.

        **A missed visit is not attendance.** This is only legal from
        `visit_booked`, so a visit whose date passed without anybody attending has
        to be rebooked rather than marked attended — which is why `scheduled_at`
        having gone by is never treated as arrival.

        **Calling it again on an attended visit is a correction, not a second
        arrival.** The evidence counts land here and they arrive late: the adjuster
        says forty-one photographs on the telephone and the report shows forty-four.
        Refusing the correction would either freeze the wrong figure or push the desk
        into recording a second visit that never happened.

        The evidence counts are what the adjuster collected. Absent means unchanged
        rather than zero: a desk correcting the photograph count should not silently
        wipe the statements.
        """
        inspection = await self._require(claim)

        correcting = inspection.status == InspectionStatus.IN_PROGRESS
        if not correcting and not rules.can_transition(
            inspection.status, InspectionStatus.IN_PROGRESS
        ):
            raise ConflictError(
                f"An inspection that is {inspection.status.replace('_', ' ')} cannot be "
                "recorded as attended. Book the visit first."
            )

        before = {"status": inspection.status, "attended_at": _iso(inspection.attended_at)}
        inspection.status = str(InspectionStatus.IN_PROGRESS)
        inspection.attended_at = attended_at
        if summary is not None:
            inspection.summary = summary
        for field, value in (
            ("photographs", photographs),
            ("measurements", measurements),
            ("statements", statements),
            ("documents", documents),
        ):
            if value is not None:
                setattr(inspection, field, max(0, value))

        # As above: pushed so the rebuilt section reads the new dates and counts.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_ATTENDED,
            summary=(
                f"{actor} corrected the attendance record for {attended_at:%d %b %Y}."
                if correcting
                else f"{inspection.adjuster_name or 'The adjuster'} attended on "
                f"{attended_at:%d %b %Y}."
            ),
            actor=actor,
            before=before,
            after={"status": inspection.status, "attended_at": _iso(attended_at)},
        )
        return inspection

    async def set_status(
        self, claim: Claim, *, status: InspectionStatus, actor: str, reason: str | None = None
    ) -> ClaimInspection:
        """Move the inspection, for the transitions the other methods do not cover.

        Chiefly `in_progress → more_needed` (the adjuster needs a second visit or a
        specialist) and `→ completed` (the report is in and accepted). Both are
        judgements a person makes about a report, which is why neither falls out of
        recording a date.
        """
        inspection = await self._require(claim)

        if inspection.status == str(status):
            raise ConflictError(f"The inspection is already {str(status).replace('_', ' ')}.")
        if not rules.can_transition(inspection.status, status):
            raise ConflictError(
                f"An inspection that is {inspection.status.replace('_', ' ')} cannot move to "
                f"{str(status).replace('_', ' ')}."
            )

        before = inspection.status
        inspection.status = str(status)

        if status is InspectionStatus.MORE_NEEDED:
            #: Sending it back is the mirror of the adjuster filing it, and both
            #: halves have to move. `returned_at` is never cleared — it is the
            #: durable answer to "has this been sent back", which has to survive the
            #: adjuster picking the work up again. `filed_at` *is* cleared, because
            #: the report is owed once more and the adjuster's own queue reads that
            #: column to decide whose desk it is on.
            inspection.returned_at = datetime.now(UTC)
            inspection.filed_at = None

        # As above.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_STATUS_CHANGED,
            summary=(
                f"{actor} moved the inspection to {str(status).replace('_', ' ')}"
                + (f": {reason}" if reason else ".")
            ),
            actor=actor,
            before={"status": before},
            after={"status": inspection.status},
            context={"reason": reason} if reason else {},
        )
        return inspection

    # -- What the visit found ------------------------------------------------

    async def add_observation(
        self,
        claim: Claim,
        *,
        element: str,
        severity: DamageSeverity,
        finding: str,
        actor: str,
        quantified_minor: int | None = None,
        currency: str | None = None,
        photo_count: int = 0,
    ) -> ClaimInspectionObservation:
        """Record one thing the adjuster looked at.

        Only once somebody has actually attended — `ATTENDED_STATUSES` includes
        `completed`, because a supplementary observation a week after the report is
        normal and refusing it would push a real finding into a free-text note.

        `quantified_minor` is optional and its currency comes with it or not at all:
        a costed element needs both numbers, and an amount with no currency is not
        an amount. A null is "not costed" rather than zero — see
        `app.domain.inspection.quantified_minor`.

        **And the currency has to be the one the claim is booked in.** Two
        currencies in one schedule make its total either a fabrication or an FX
        conversion, and this system holds no rate to do the second with.
        """
        inspection = await self._require(claim)

        if not rules.has_attended(inspection.status):
            raise ConflictError(
                "Nothing has been inspected yet. Record the visit as attended before "
                "logging what was found."
            )
        if (quantified_minor is None) != (currency is None):
            raise ValidationError(
                "Give the amount and its currency together, or neither. A figure with no "
                "currency is not an amount."
            )

        booking = (claim.currency or "").upper()
        if currency is not None and booking and currency.upper() != booking:
            #: **The schedule is kept in the currency the claim is booked in.**
            #:
            #: Not pedantry: the tab and the adjuster's board both total this
            #: schedule, and a total across two currencies is either a lie or an
            #: FX conversion — and the rate that would relate them is a fact this
            #: system does not hold, which is the same reason the reserve ledger's
            #: accounting pair is nullable. Refusing here is what keeps the total
            #: labelled with the money it is actually in.
            raise ValidationError(
                f"This claim is booked in {booking}, and the schedule has to be too. "
                f"Recording {currency.upper()} against it would make the total a "
                "figure in neither currency."
            )

        observation = self._claims.add_observation(
            ClaimInspectionObservation(
                inspection_id=inspection.id,
                element=element,
                severity=str(severity),
                finding=finding,
                quantified_minor=quantified_minor,
                currency=currency.upper() if currency else None,
                photo_count=max(0, photo_count),
                recorded_by=actor,
            )
        )

        # So the observation has an id and the rebuilt schedule includes it.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_OBSERVED,
            summary=(
                f"{actor} recorded {element} as {str(severity).replace('_', ' ')}"
                + (
                    f", costed at {_major(quantified_minor)} {observation.currency}."
                    if quantified_minor is not None
                    else "."
                )
            ),
            actor=actor,
            after={
                "element": element,
                "severity": str(severity),
                "quantified_minor": quantified_minor,
            },
        )
        return observation

    # -- The work it leaves behind -------------------------------------------

    async def add_action(
        self,
        claim: Claim,
        *,
        label: str,
        owner: str,
        actor: str,
        due_at: datetime | None = None,
    ) -> ClaimInspectionAction:
        """Raise something that has to happen before the inspection is done with.

        `owner` is a name, not a handler id: the owner is as often the adjuster, the
        insured or a contractor as somebody on the desk — see
        `ClaimInspectionAction`.
        """
        inspection = await self._require(claim)

        action = self._claims.add_inspection_action(
            ClaimInspectionAction(
                inspection_id=inspection.id,
                label=label,
                owner=owner,
                due_at=due_at,
                done=False,
                raised_by=actor,
            )
        )
        # So the action has an id and the rebuilt list includes it.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_ACTION_SET,
            summary=f"{actor} raised an inspection action for {owner}: {label}",
            actor=actor,
            after={"label": label, "owner": owner, "due_at": _iso(due_at)},
        )
        return action

    async def set_action_done(
        self, claim: Claim, *, action_id: uuid.UUID, done: bool, actor: str
    ) -> ClaimInspectionAction:
        """Tick or untick one action.

        Un-ticking is allowed and is not an oversight: a follow-up marked complete
        that turns out not to be is a normal correction, and a one-way tick would
        make the desk raise a duplicate action to say so.
        """
        inspection = await self._require(claim)
        action = await self._claims.get_inspection_action(inspection.id, action_id)
        if action is None:
            raise NotFoundError(f"{claim.reference} has no inspection action {action_id}.")

        if action.done == done:
            return action

        action.done = done
        action.done_by = actor if done else None
        action.done_at = datetime.now(UTC) if done else None

        # So the rebuilt list reads the tick rather than the row behind it.
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_ACTION_SET,
            summary=(
                f"{actor} marked '{action.label}' as " + ("done." if done else "outstanding again.")
            ),
            actor=actor,
            after={"label": action.label, "done": done},
        )
        return action

    # -- Shared --------------------------------------------------------------

    async def _require(self, claim: Claim) -> ClaimInspection:
        inspection = await self._claims.get_inspection(claim.id)
        if inspection is None:
            raise NotFoundError(
                f"No inspection has been commissioned on {claim.reference}. "
                "Commission one before booking or recording a visit."
            )
        return inspection


def _iso(value: datetime | None) -> str | None:
    """A timestamp for an audit payload. JSONB holds no datetimes."""
    return value.isoformat() if value else None


def _major(amount_minor: int) -> str:
    """Minor units as a figure a person reads, with thousands separated.

    The summary is prose on the activity log, and `18000000` in the middle of a
    sentence is a number nobody can check at a glance — which is the one thing an
    audit line has to be. No currency symbol: the code travels beside it, and this
    module has no business knowing which symbol goes with which.
    """
    return f"{amount_minor / 100:,.2f}"


__all__ = ["ClaimInspectionService"]
