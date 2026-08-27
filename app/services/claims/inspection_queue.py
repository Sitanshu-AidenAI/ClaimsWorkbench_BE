"""The loss adjuster's board: the queue of visits, and one report.

The read side of the inspection, turned ninety degrees. `ClaimSectionsService`
answers *what is happening on this claim*; this answers *what have I been asked to
go and look at*, which is a different question over the same rows and belongs to a
different person.

**Whose work this is, and why the payload admits it.** `claim_inspections`
identifies the adjuster by `adjuster_name` — a free-text name, because a firm is
instructed before a person is named and neither has an account on this system. So
"commissioned to me" can only be resolved by matching that string against the
signed-in person's name, which works for an internal adjuster and cannot work for
Crawford & Co. Rather than quietly widening the board to everybody's work while it
still says *commissioned to you*, the service reports `whole_desk` and the page
says which it is showing. A queue that misrepresents whose work it is, is worse
than one that admits the limitation.

Nothing here writes except `file_report`, and that does not commit — the route owns
the transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import ConflictError, NotFoundError
from app.domain import inspection as rules
from app.domain.enums import AuditEventType, InspectionStatus
from app.models.claim import Claim, ClaimInspection
from app.repositories.claim import ClaimRepository
from app.schemas import inspections as api
from app.schemas.fnol import money
from app.services.fnol.audit import AuditService

#: What the board is for, stated by the server. On the page under the title.
_DESCRIPTION = (
    "Site visits commissioned to you — investigate the loss, quantify the damage, "
    "and send a recommendation back to the handler."
)

#: The same, when the board is showing everybody's work. Said plainly rather than
#: leaving an adjuster to assume the four rows in front of them are all of theirs.
_DESCRIPTION_DESK = (
    "Every site visit commissioned on the desk. Adjusters are recorded by name "
    "rather than by account, so this board cannot yet be narrowed to your own."
)

#: The chips, in the order the board draws them, with the words it draws them in.
_CHIP_LABELS: tuple[tuple[str, str], ...] = (
    ("to_do", "To do"),
    ("to_schedule", "To schedule"),
    ("booked_in_progress", "Booked & in progress"),
    ("sent_back", "Sent back"),
    ("filed", "Filed"),
    ("all", "All"),
)

#: What a commercial adjuster's report carries that this record does not.
#:
#: Stated once, here, and sent to the screen. Each line is a thing a handler would
#: reasonably look for and not find, and naming them is what stops the pane drawing
#: an empty panel that reads as a fault. Deleting a line here is what removes the
#: notice from the screen.
_NOT_RECORDED = (
    "A liability finding — who the adjuster holds at fault, and on what split.",
    "A settlement recommendation with a figure and a rationale.",
    "A damage schedule priced by category, quantity and unit cost against a rate "
    "card. What is recorded is what the adjuster observed on site.",
    "Salvage and subrogation potential, which are recovery questions this system does not track.",
)


class ClaimInspectionQueueService:
    def __init__(self, claims: ClaimRepository, audit: AuditService) -> None:
        self._claims = claims
        self._audit = audit

    # -- The queue -----------------------------------------------------------

    async def queue(
        self, *, chip: str, adjuster_name: str | None = None, now: datetime | None = None
    ) -> api.InspectionQueueOut:
        """Every commissioned visit, under one chip, with the counts for all six.

        The rows are read once and filtered in Python rather than per chip in SQL,
        and that is deliberate: the six facet counts and the four metric tiles are
        computed over the *same* set as the list, so a handler pressing *Sent back*
        cannot be shown a count that disagrees with what appears. Six `COUNT(*)`
        queries could each be true of a slightly different instant.

        `now` is a parameter because *overdue* is the only fact on this board that
        depends on the clock, and a queue whose lateness shifts between the tile and
        the row it is counting would be unreadable.
        """
        moment = now or datetime.now(UTC)
        rows = await self._claims.inspection_queue(adjuster_name=adjuster_name)

        built = [
            self._row(inspection, claim, priced=priced, priced_currency=currency, now=moment)
            for inspection, claim, priced, currency, _open_actions in rows
        ]
        pairs = list(zip(built, (row[0] for row in rows), strict=True))

        shown = [out for out, inspection in pairs if rules.matches_chip(inspection, chip)]
        facets = [
            api.QueueFacetOut(
                id=identifier,
                label=label,
                count=sum(
                    1 for _, inspection in pairs if rules.matches_chip(inspection, identifier)
                ),
            )
            for identifier, label in _CHIP_LABELS
        ]

        return api.InspectionQueueOut(
            items=shown,
            #: The whole book behind the active chip, which is what the footer
            #: reports. Equal to `len(items)` until this endpoint starts paging, and
            #: read from the facet so the two cannot drift when it does.
            total=next((facet.count for facet in facets if facet.id == chip), len(shown)),
            metrics=self._metrics(built),
            facets=facets,
            description=_DESCRIPTION if adjuster_name else _DESCRIPTION_DESK,
            alert_note=_alert(built),
            whole_desk=adjuster_name is None,
        )

    def _row(
        self,
        inspection: ClaimInspection,
        claim: Claim,
        *,
        priced: int,
        priced_currency: str | None,
        now: datetime,
    ) -> api.InspectionRowOut:
        return api.InspectionRowOut(
            id=inspection.id,
            reference=claim.reference,
            claimant=claim.claimant_name or claim.insured_name or "Not recorded",
            loss_description=claim.loss_description or "No description on the notice.",
            date_of_loss=claim.date_of_loss,
            #: The site the adjuster was given, or where the loss happened. The
            #: second is the default the visit was commissioned against.
            site_address=inspection.site_address or claim.loss_location,
            due_at=inspection.report_due_at,
            overdue=rules.is_overdue(inspection, report_due_at=inspection.report_due_at, now=now),
            #: Absent rather than zero when nothing has been priced. A loss that
            #: cost nothing and a loss nobody has costed are opposite facts, and a
            #: column of £0 on this board would be read as the first.
            quantified=money(priced, priced_currency) if priced and priced_currency else None,
            status=inspection.status,
            visit_at=inspection.scheduled_at,
            sent_back=inspection.returned_at is not None,
            filed_at=inspection.filed_at,
            priority=claim.priority,
        )

    def _metrics(self, rows: list[api.InspectionRowOut]) -> list[api.QueueMetricOut]:
        """The four tiles, counted over every visit rather than the active chip.

        A tile that changed when a chip was pressed would be a filter, and the row
        of chips underneath it is already the filter. These are the standing shape
        of the adjuster's book.
        """
        counted = {
            "to_schedule": sum(
                1
                for row in rows
                if row.filed_at is None and row.status == InspectionStatus.TO_SCHEDULE
            ),
            "in_progress": sum(
                1
                for row in rows
                if row.filed_at is None and row.status == InspectionStatus.IN_PROGRESS
            ),
            "overdue": sum(1 for row in rows if row.overdue),
            "filed": sum(1 for row in rows if row.filed_at is not None),
        }
        labels = {
            "to_schedule": "To schedule",
            "in_progress": "Visits in progress",
            "overdue": "Overdue",
            "filed": "Reports filed",
        }
        return [
            api.QueueMetricOut(id=key, label=labels[key], display=str(value))
            for key, value in counted.items()
        ]

    # -- One report ----------------------------------------------------------

    async def report(self, reference: str, *, now: datetime | None = None) -> api.ReportOut:
        """One inspection, by the claim reference the board put in its URL."""
        moment = now or datetime.now(UTC)
        found = await self._claims.inspection_by_claim_reference(reference)
        if found is None:
            raise NotFoundError(
                f"No inspection has been commissioned on {reference}. A visit has to be "
                "commissioned from the claim before there is a report to work."
            )

        inspection, claim = found
        observations = list(await self._claims.list_observations(inspection.id))
        actions = list(await self._claims.list_inspection_actions(inspection.id))

        #: The currency comes out of the observations, not off the claim. Stamping
        #: the claim's booking currency on this sum reported a GBP schedule on a US
        #: claim as dollars — same number, different money, no warning.
        total = rules.quantified_total(observations)
        return api.ReportOut(
            id=inspection.id,
            reference=claim.reference,
            claimant=claim.claimant_name or claim.insured_name or "Not recorded",
            status=inspection.status,
            next_statuses=sorted(
                str(status) for status in rules.allowed_transitions(inspection.status)
            ),
            reference_number=inspection.reference,
            adjuster_name=inspection.adjuster_name,
            adjuster_firm=inspection.adjuster_firm,
            commissioned_by=inspection.commissioned_by,
            commissioned_at=inspection.commissioned_at,
            due_at=inspection.report_due_at,
            overdue=rules.is_overdue(
                inspection, report_due_at=inspection.report_due_at, now=moment
            ),
            sent_back=inspection.returned_at is not None,
            filed_at=inspection.filed_at,
            visit=api.ReportVisitOut(
                scheduled_at=inspection.scheduled_at,
                attended_at=inspection.attended_at,
                site_kind=inspection.site_kind,
                site_address=inspection.site_address or claim.loss_location,
                site_identifier=inspection.site_identifier,
                contact_name=inspection.site_contact_name,
                contact_phone=inspection.site_contact_phone,
                access_note=inspection.site_access_note,
                #: What the claim says caused the loss — what the adjuster was
                #: told, not what they concluded. Their conclusion is the summary
                #: and the observations beneath it.
                reported_cause=claim.loss_type,
            ),
            summary=inspection.summary,
            observations=[
                api.ReportObservationOut(
                    id=observation.id,
                    element=observation.element,
                    severity=observation.severity,
                    finding=observation.finding,
                    quantified=money(observation.quantified_minor, observation.currency)
                    if observation.quantified_minor is not None and observation.currency
                    else None,
                    photo_count=observation.photo_count,
                )
                for observation in observations
            ],
            quantified_total=money(total[0], total[1]) if total else None,
            priced_count=rules.priced_count(observations),
            evidence=api.ReportEvidenceOut(
                photographs=inspection.photographs,
                measurements=inspection.measurements,
                statements=inspection.statements,
                documents=inspection.documents,
            ),
            actions=[
                api.ReportActionOut(
                    id=action.id,
                    label=action.label,
                    owner=action.owner,
                    due_at=action.due_at,
                    done=action.done,
                )
                for action in actions
            ],
            filing_blockers=rules.filing_blockers(
                inspection, observations=observations, actions=actions
            ),
            not_recorded=list(_NOT_RECORDED),
        )

    # -- Filing --------------------------------------------------------------

    async def file_report(self, reference: str, *, actor: str) -> ClaimInspection:
        """Send the report to the handler.

        The adjuster's half of an exchange whose other half already existed: the
        handler could accept a report or send it back, and nothing represented its
        arrival. Filing does **not** move the status — the visit is still whatever it
        was — because filing is a fact about the report and the status is the state
        of the visit. That distinction is the same one the board's chips rest on.

        Refused on the blockers the domain computes, listed rather than summarised:
        an adjuster told "this report is not ready" has to guess which of three
        things to go and do.
        """
        found = await self._claims.inspection_by_claim_reference(reference)
        if found is None:
            raise NotFoundError(f"No inspection has been commissioned on {reference}.")

        inspection, claim = found
        if inspection.filed_at is not None:
            raise ConflictError(
                f"That report was already filed on {inspection.filed_at:%d %b %Y}. "
                "The handler has it."
            )

        observations = list(await self._claims.list_observations(inspection.id))
        actions = list(await self._claims.list_inspection_actions(inspection.id))
        blockers = rules.filing_blockers(inspection, observations=observations, actions=actions)
        if blockers:
            raise ConflictError("That report is not ready to file. " + " ".join(blockers))

        inspection.filed_at = datetime.now(UTC)
        await self._claims.flush()

        self._audit.claim(
            claim,
            event_type=AuditEventType.INSPECTION_FILED,
            summary=(
                f"{actor} filed the inspection report"
                + (f" with {inspection.adjuster_firm}." if inspection.adjuster_firm else ".")
            ),
            actor=actor,
            after={
                "filed_at": inspection.filed_at.isoformat(),
                "quantified_minor": rules.quantified_minor(observations),
                "observations": len(observations),
            },
        )
        return inspection


def _alert(rows: list[api.InspectionRowOut]) -> str | None:
    """The footer's warning, where the board has one worth making.

    Counted from the rows the queue just built rather than from a second query, so
    the sentence and the list cannot disagree about how many are late.
    """
    overdue = [row for row in rows if row.overdue]
    if not overdue:
        return None
    if len(overdue) > 1:
        return f"{len(overdue)} reports are past their due date."
    return f"One report is past its due date — {overdue[0].reference}."


__all__ = ["ClaimInspectionQueueService"]
