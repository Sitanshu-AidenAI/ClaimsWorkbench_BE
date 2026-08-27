"""The manager's approval queue.

Its own router rather than more routes under `/claims`, because it is a different
person's screen over the same records — the same reason `/inspections` is separate.

**This closed the loop.** A handler referring a claim moved it to `escalated`, and
the queue a manager works read fixtures, so the referral arrived nowhere. Both
halves of that exchange were real; only the screen joining them was not.

There is no decision endpoint here, deliberately. `POST /claims/{ref}/decision`
already owns every transition and every block, and a second decision path would be
a second set of rules to keep in step — which is the failure `approval_blocks`
exists to prevent. The sheet reads from here and writes there.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps.auth import require_roles
from app.api.deps.services import FNOLContextDep
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import CLAIM_ASSIGN_ROLES
from app.schemas import approvals as api

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])

#: **Narrower than the claims queue on purpose.** This board is a manager's own
#: work: it shows what is waiting on their signature and what they have approved
#: this month. A handler reading it would be reading their supervisor's backlog,
#: which is not information the handler's job needs — and the decisions it leads to
#: are gated to these roles anyway.
ManagerAccess = Annotated[Principal, Depends(require_roles(*CLAIM_ASSIGN_ROLES))]


@router.get("", response_model=api.ApprovalQueueOut, summary="The approval queue")
async def list_approvals(
    context: FNOLContextDep,
    principal: ManagerAccess,
    chip: Annotated[str | None, Query(pattern="^(over_authority|fraud_referral)$")] = None,
    scope: Annotated[str, Query(pattern="^(my_team|all_escalations)$")] = "all_escalations",
) -> api.ApprovalQueueOut:
    """Every escalated claim awaiting a decision, with the tiles that describe them.

    **`scope=my_team` needs the reader to be in the handler directory with a team
    recorded**, and the payload reports `whole_book` when they are not. Narrowing
    silently while calling the queue *your decision* would misstate whose work it
    is — the same honesty the inspection board's `whole_desk` carries.

    Unassigned escalations appear under every scope. They belong to nobody's team,
    and hiding them would hide the claims that most need a decision: the ones with
    no handler to make it.

    The two chips narrow by *what is blocking* rather than by status, because every
    row here is escalated and a status facet would be a row of ones.
    """
    return await context.approvals.queue(
        chip=chip,
        subject=principal.subject if scope == "my_team" else None,
    )


@router.get(
    "/{reference}",
    response_model=api.ApprovalDetailOut,
    summary="One approval sheet",
)
async def get_approval(
    reference: str, context: FNOLContextDep, principal: ManagerAccess
) -> api.ApprovalDetailOut:
    """The request, argued: the figure asked for and the numbers behind it.

    `conditions` are the same blocks the handler's own decision bar renders, phrased
    for a manager and including the ones that were **checked and passed** — "the
    fraud review is clear" is worth reading before signing a large settlement, and a
    list of only problems leaves a manager unable to tell a checked claim from an
    unchecked one.

    `authority_limit` is the *reader's* own, so the sheet names the figure this is
    measured against. Null where they have none recorded, which is a fact worth
    showing rather than a zero.
    """
    return await context.approvals.sheet(reference, subject=principal.subject)


__all__ = ["router"]
