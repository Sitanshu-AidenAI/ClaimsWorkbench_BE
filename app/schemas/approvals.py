"""The manager's approval queue: escalated claims awaiting a decision.

A separate domain from the claims queue rather than a filter on it, because a
manager is not looking at claims — they are looking at **requests**: a figure a
handler has asked them to sign, the reason it left that handler's desk, and how
long it has been sitting. The claim underneath is context.

**Every row here is escalated**, which is why nothing in these shapes carries a
status facet: it would be a row of ones. What varies is *what is blocking*, and
that is what the chips narrow on.

The shapes mirror `ClaimsWorkbench_FE/src/types/approvals.ts`, which is where they
were designed — and which read fixtures until this module existed, so a claim a
handler escalated appeared on no manager's queue at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas.common import SchemaBase
from app.schemas.fnol import Money


class ApprovalRowOut(SchemaBase):
    """One escalation waiting on a manager."""

    id: uuid.UUID
    reference: str
    claimant: str
    location: str
    loss_type: str
    #: Who escalated it, from the assignment. "Unassigned" is possible and is
    #: itself one of the blocking conditions.
    handler: str
    #: How long the request has waited on a manager, from the audit event that
    #: escalated it. The queue's own SLA measure.
    waiting_days: int
    #: The figure the handler is asking to be signed off — the claim's reserve.
    requested: Money
    #: `fraud_and_authority`, `over_authority` or `settlement`. Derived from the
    #: two flags rather than stored: it is a reading of the claim, not a fact
    #: somebody recorded, and storing it would let it drift from the flags.
    reason: str
    priority: str
    over_authority: bool
    fraud_flag: bool


class ApprovalMetricOut(SchemaBase):
    """One tile above the list.

    `display` is already a string. Abbreviation is a server concern — `£259,550`
    and `6d` are formatting decisions that belong beside the figures they describe,
    and a client that rounded them itself would eventually round one differently.
    """

    id: str
    label: str
    display: str


class ApprovalFacetOut(SchemaBase):
    id: str
    label: str
    count: int


class ApprovalQueueOut(SchemaBase):
    items: list[ApprovalRowOut]
    total: int
    metrics: list[ApprovalMetricOut]
    facets: list[ApprovalFacetOut]
    description: str
    #: The footer's one useful sentence: how long the queue takes to clear.
    clearance_note: str
    #: True when `my_team` could not be resolved and the whole book is being shown.
    #: See `ClaimApprovalService.queue` — a manager with no directory row has no
    #: team to narrow to, and a queue that silently widened would misrepresent
    #: whose escalations these are.
    whole_book: bool


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


class ApprovalSettlementOut(SchemaBase):
    """The money, broken out the way an approval is argued.

    `recoverable` is what the recovery register expects to get back, and
    `net_cost` is what the book carries once it lands. Both are real figures from
    `claim_recoveries` — they were zero while recoveries were unbuilt, and a zero
    there meant "not tracked" rather than "nothing to recover", which is the
    distinction that made it worth building.
    """

    requested: Money
    claimed: Money
    #: Deducted, so the sheet shows it negative.
    excess: Money
    recoverable: Money
    net_cost: Money


class ApprovalActionOut(SchemaBase):
    """A named way to deal with a blocking condition.

    `proposed` is true when there is no endpoint behind it. The sheet still offers
    it and says so, rather than implying the workflow is complete — the same rule
    the rest of this codebase follows about naming what is missing.
    """

    id: str
    label: str
    proposed: bool


class ApprovalConditionOut(SchemaBase):
    """Whether something stands between the manager and the approve button.

    Two states and no third. A condition a manager cannot act on is not a
    condition, it is a note — so `cleared` conditions are listed too, because "the
    fraud review is clear" is worth reading before signing $3.7m.
    """

    id: str
    label: str
    detail: str
    #: `blocking` or `cleared`.
    state: str
    actions: list[ApprovalActionOut]


class ApprovalCoverageOut(SchemaBase):
    """The assistant's verdict on cover, as it stood when the claim was created."""

    verdict: str
    summary: str


class ApprovalNoteOut(SchemaBase):
    """The handler's reasoning, shown verbatim — a person wrote it for a person."""

    author: str
    #: Computed here rather than sliced from the name on the client, which breaks
    #: on a great many of them.
    initials: str
    written_at: datetime
    body: str


class ApprovalDetailOut(SchemaBase):
    id: uuid.UUID
    reference: str
    claimant: str
    status: str
    escalated_by: str
    escalated_days_ago: int
    settlement: ApprovalSettlementOut
    #: The **manager's** own limit, so the sheet names the figure this is measured
    #: against. Null when the reader has no authority recorded, which is a fact
    #: worth showing rather than a zero.
    authority_limit: Money | None
    conditions: list[ApprovalConditionOut]
    coverage: ApprovalCoverageOut | None
    handler_note: ApprovalNoteOut | None


__all__ = [
    "ApprovalActionOut",
    "ApprovalConditionOut",
    "ApprovalCoverageOut",
    "ApprovalDetailOut",
    "ApprovalFacetOut",
    "ApprovalMetricOut",
    "ApprovalNoteOut",
    "ApprovalQueueOut",
    "ApprovalRowOut",
    "ApprovalSettlementOut",
]
