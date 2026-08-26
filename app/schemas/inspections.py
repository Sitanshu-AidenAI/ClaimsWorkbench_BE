"""The loss adjuster's board: the queue of visits, and one report.

The third persona's endpoint. `claims.py` is what a handler reads about a claim and
`claim_sections.py` is what they read around it; this is what the *adjuster* reads
about the visits commissioned to them — the same records, turned ninety degrees.

**Two of this board's six chips are not statuses**, and the schema keeps the
distinction the frontend's `types/inspection.ts` was written around. `status` is the
state of the *visit*; `sent_back` and `filed_at` are facts about the *report*. An
inspection returned last week and being worked again today is `in_progress` **and**
sent back, and a fifth status would have made those two mutually exclusive.

**What is honestly absent is absent.** A commercial adjuster's report carries a
liability finding, a settlement recommendation and a damage schedule priced against
a rate card. None of those is modelled here, so `ReportOut` says which of its
sections are backed by records rather than shipping a plausible liability finding
that nobody made — the same rule `claim_sections.py` follows and for the same
reason: a fabricated finding on this screen is one an adjuster would be asked to
defend in a deposition.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas.common import SchemaBase
from app.schemas.fnol import Money


class InspectionRowOut(SchemaBase):
    """One row on the adjuster's queue.

    The claim's facts and the visit's facts side by side, because that is the
    decision an adjuster makes from this list: *which of these do I drive to next*.
    So the site is here and the policy number is not.
    """

    id: uuid.UUID
    #: The claim's reference, not the inspection's id. It is the number every party
    #: to the loss already has, and what the board puts in its URL.
    reference: str
    claimant: str
    loss_description: str
    date_of_loss: datetime | None
    #: The site as an adjuster would put it into a satnav. Falls back to where the
    #: loss happened, which is what the visit was commissioned against.
    site_address: str | None
    #: When the report is owed. Null where the desk promised nothing.
    due_at: datetime | None
    #: The date has passed and nothing has been filed. Never true without a due date.
    overdue: bool
    #: The damage schedule's total. **Null until something has been priced** — not
    #: zero, which would read as a loss that cost nothing.
    quantified: Money | None
    status: str
    #: When the visit is booked for, where one is booked.
    visit_at: datetime | None
    #: A handler has returned this report at least once. Not a status — see the
    #: module docstring. Survives the adjuster picking the work up again.
    sent_back: bool
    #: When the report went to the handler. Null while it is still the adjuster's.
    filed_at: datetime | None
    priority: str


class QueueMetricOut(SchemaBase):
    """One tile above the queue. `display` is already a string, deliberately.

    The tiles show counts today and a tile showing "3 late" or a duration tomorrow
    should not require a client change. The server owns the wording of its own
    figures — the same reason the claims queue's metrics are pre-resolved.
    """

    id: str
    label: str
    display: str


class QueueFacetOut(SchemaBase):
    """One chip, and how many sit under it.

    Counted over the whole book rather than the returned page, because the number on
    a chip is what tells an adjuster whether to press it.
    """

    id: str
    label: str
    count: int


class InspectionQueueOut(SchemaBase):
    items: list[InspectionRowOut]
    #: How many sit under the active chip, which is not `len(items)` the moment this
    #: endpoint starts paging.
    total: int
    metrics: list[QueueMetricOut]
    facets: list[QueueFacetOut]
    #: What this board is for, in the server's words. On the page under the title.
    description: str
    #: The footer's warning, where there is one. Null means nothing is late, which
    #: the footer states positively rather than leaving blank.
    alert_note: str | None
    #: True when the queue is every adjuster's work rather than the reader's own.
    #: See `ClaimInspectionQueueService.queue` — the adjuster is a name on a row and
    #: not an account, so a board that silently showed the whole desk while looking
    #: personal would misrepresent whose work it is.
    whole_desk: bool


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class ReportVisitOut(SchemaBase):
    """The visit itself: when, where, and who met the adjuster."""

    scheduled_at: datetime | None
    #: Null until somebody has actually been. One of the filing blockers.
    attended_at: datetime | None
    site_kind: str | None
    site_address: str | None
    site_identifier: str | None
    contact_name: str | None
    contact_phone: str | None
    access_note: str | None
    #: What the claim says caused the loss. The adjuster's own view of the cause is
    #: their summary and their observations; this is what they were told.
    reported_cause: str | None


class ReportObservationOut(SchemaBase):
    """One element the adjuster looked at.

    **This is the damage schedule, and it is a schedule of observations rather than
    of priced lines.** A commercial schedule has categories, quantities and unit
    costs against a rate card; what this system holds is what the adjuster recorded
    on site. Presenting the latter as the former would mean inventing a quantity of
    one and a unit cost equal to the total for every row, which is a rate card made
    of nothing.
    """

    id: uuid.UUID
    element: str
    severity: str
    finding: str
    #: Absent means *not costed on site*, which is different from zero.
    quantified: Money | None
    photo_count: int


class ReportActionOut(SchemaBase):
    id: uuid.UUID
    label: str
    owner: str
    due_at: datetime | None
    done: bool


class ReportEvidenceOut(SchemaBase):
    photographs: int
    measurements: int
    statements: int
    documents: int


class ReportOut(SchemaBase):
    """One inspection report, as the adjuster works it.

    `filing_blockers` is the list the *server* computed, and the pane draws it. The
    button is disabled from the same list, but the API refuses on it independently:
    a disabled button is a courtesy and not a guarantee, and an adjuster who reaches
    the endpoint with three actions open gets the reasons rather than a silent
    success.
    """

    id: uuid.UUID
    reference: str
    claimant: str
    status: str
    #: Where the visit may go next, from `app.domain.inspection`. Sent rather than
    #: reimplemented, for the reason the workbench tab's copy explains.
    next_statuses: list[str]
    reference_number: str | None
    adjuster_name: str | None
    adjuster_firm: str | None
    commissioned_by: str
    commissioned_at: datetime
    due_at: datetime | None
    overdue: bool
    sent_back: bool
    filed_at: datetime | None

    visit: ReportVisitOut
    #: The adjuster's own summary of what they found. Null before the visit.
    summary: str | None
    observations: list[ReportObservationOut]
    #: The sum of the priced, affected observations. Null when nothing is priced.
    quantified_total: Money | None
    #: How many observations carry a figure, beside the total — so `£180,000` is
    #: legible as the sum of two priced rows out of eleven rather than as the
    #: assessed cost of the loss.
    priced_count: int
    evidence: ReportEvidenceOut
    actions: list[ReportActionOut]

    #: Empty means the report can be filed. Sentences, because a person reads them.
    filing_blockers: list[str]
    #: What a full adjuster's report carries and this record does not. Named so the
    #: pane can say so instead of drawing an empty panel that looks broken — and so
    #: that deleting a line here is what removes the notice, rather than a string
    #: hunt through React.
    not_recorded: list[str]


__all__ = [
    "InspectionQueueOut",
    "InspectionRowOut",
    "QueueFacetOut",
    "QueueMetricOut",
    "ReportActionOut",
    "ReportEvidenceOut",
    "ReportObservationOut",
    "ReportOut",
    "ReportVisitOut",
]
