"""The loss adjuster's board.

Its own router rather than more routes under `/claims`, because it is a different
person's screen over the same records. A handler asks *what is happening on this
claim*; an adjuster asks *what have I been asked to go and look at*, and the second
question has no claim reference in it until an answer comes back.

**Reading is `FNOL_READ_ROLES`, which includes the loss adjuster; filing is
`INSPECTION_WORK_ROLES`, which is the first write this product gives them.** Every
other claim write excludes the adjuster on the grounds that they read the file
rather than write to it. That was right while there was no inspection record and is
wrong now: the report is the adjuster's own work product, and a system where the
handler has to type it up on their behalf is one where the audit trail attributes
the adjuster's findings to somebody else.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps.auth import require_roles
from app.api.deps.services import FNOLContextDep
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import FNOL_READ_ROLES, INSPECTION_WORK_ROLES
from app.schemas import inspections as api

logger = get_logger(__name__)

router = APIRouter(prefix="/inspections", tags=["inspections"])

ReadAccess = Annotated[Principal, Depends(require_roles(*FNOL_READ_ROLES))]
WorkAccess = Annotated[Principal, Depends(require_roles(*INSPECTION_WORK_ROLES))]


@router.get("", response_model=api.InspectionQueueOut, summary="The field inspection queue")
async def list_inspections(
    context: FNOLContextDep,
    principal: ReadAccess,
    chip: Annotated[
        str, Query(pattern="^(to_do|to_schedule|booked_in_progress|sent_back|filed|all)$")
    ] = "to_do",
    mine: Annotated[bool, Query()] = False,
) -> api.InspectionQueueOut:
    """Every visit commissioned, under one chip, with the counts for all six.

    **`mine` is off by default and the payload says which it gave you.** It now
    narrows on *identity* rather than on a display name: a visit is the reader's when
    its `adjuster_subject` is their account, or — before they have recorded anything
    — when its `adjuster_email` is their address. A visit instructed to a firm with
    no named contact belongs to nobody and appears only on the whole desk.

    Still off by default, and `whole_desk` is still reported, because the default has
    to be the honest one for a reader whose visits carry no address: `mine` would
    show them an empty board under the heading *commissioned to you*, which reads as
    "you have no work" rather than as "nobody wrote your address down".

    Two of the six chips are not statuses. `sent_back` and `filed` are facts about
    the report; the other four are states of the visit. See
    `app.domain.inspection.matches_chip`.
    """
    return await context.inspection_queue.queue(
        chip=chip,
        adjuster_subject=principal.subject if mine else None,
        adjuster_email=principal.email if mine else None,
    )


@router.get(
    "/{reference}",
    response_model=api.ReportOut,
    summary="One inspection report",
)
async def get_inspection(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> api.ReportOut:
    """The report for the inspection on one claim.

    Addressed by the **claim's** reference rather than the inspection's id, because
    that is the number every party to the loss already has and there is one
    inspection per claim.

    `not_recorded` names what a full commercial report carries and this record does
    not — the liability finding, the settlement recommendation, the rate-card
    schedule. The pane draws that list rather than four empty panels, which is the
    same choice `claim_sections.py` makes for the recovery register.
    """
    del principal
    return await context.inspection_queue.report(reference)


@router.post(
    "/{reference}/filing",
    response_model=api.ReportOut,
    summary="File the report with the handler",
)
async def file_inspection_report(
    reference: str, context: FNOLContextDep, principal: WorkAccess
) -> api.ReportOut:
    """Send the report to the handler.

    The adjuster's half of an exchange whose other half already existed — the
    handler could accept a report or return it, and nothing recorded its arrival.

    Filing does not move the status: the visit is still whatever it was. It is a
    fact about the report, which is the same distinction the board's chips rest on.

    Refused with the blockers named rather than summarised. An adjuster told only
    that their report "is not ready" has to guess which of three things to go and
    do, and `app.domain.inspection.filing_blockers` already knows which.
    """
    await context.inspection_queue.file_report(reference, actor=_actor(principal))
    await context.commit()
    return await context.inspection_queue.report(reference)


def _actor(principal: Principal) -> str:
    return principal.full_name or principal.username or principal.email or principal.subject


__all__ = ["router"]
