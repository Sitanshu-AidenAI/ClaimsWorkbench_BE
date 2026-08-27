"""The inspection's state machine, and the arithmetic over what an adjuster found.

The third of these modules — `lifecycle.py` for the notice, `claim_lifecycle.py`
for the claim, this for the visit — and deliberately the same shape, so a reader
who knows one knows all three. Pure functions over values: no session, no ORM
beyond attribute reads, no clock.

Five rules live here and nowhere else:

* which transitions are legal (`can_transition`), so booking a visit on a claim
  nobody commissioned is a 409 rather than a row that makes no sense;
* what the visit found, in money (`quantified_minor`), which is the figure a
  handler reserves against and must not be a sum of the wrong rows;
* what is still outstanding (`outstanding_actions`), which is what the tab's badge
  counts and what stops a completed inspection reading as finished when three
  follow-ups are open;
* what stands between the adjuster and filing (`filing_blockers`), which the
  adjuster's own screen draws as a list and the API enforces — a disabled button is
  a courtesy, not a guarantee; and
* which chip on the adjuster's queue an inspection sits under (`matches_chip`),
  which is where the distinction between a *status* and a *fact about the report*
  is actually made; and
* whose visit it is (`owns_inspection`, `may_record_findings`), which is what lets
  a loss adjuster record their own findings without being able to record anybody
  else's.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from app.domain.enums import CLAIM_WORK_ROLES, DamageSeverity, InspectionStatus

# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

#: Legal transitions. Absent keys are terminal.
#:
#: Two edges are worth reading twice. `VISIT_BOOKED → TO_SCHEDULE` is a rebooking,
#: and it has to exist because adjusters and insureds cancel — without it the desk
#: would record a cancelled visit as attended or leave it booked for a date that
#: has passed. And `MORE_NEEDED → VISIT_BOOKED` is the second visit, which is the
#: normal outcome on a large loss rather than an exception.
_ALLOWED: dict[InspectionStatus, frozenset[InspectionStatus]] = {
    InspectionStatus.NOT_COMMISSIONED: frozenset({InspectionStatus.TO_SCHEDULE}),
    InspectionStatus.TO_SCHEDULE: frozenset(
        {InspectionStatus.VISIT_BOOKED, InspectionStatus.NOT_COMMISSIONED}
    ),
    InspectionStatus.VISIT_BOOKED: frozenset(
        {InspectionStatus.IN_PROGRESS, InspectionStatus.TO_SCHEDULE}
    ),
    InspectionStatus.IN_PROGRESS: frozenset(
        {InspectionStatus.COMPLETED, InspectionStatus.MORE_NEEDED}
    ),
    InspectionStatus.MORE_NEEDED: frozenset(
        {InspectionStatus.VISIT_BOOKED, InspectionStatus.COMPLETED}
    ),
}

#: Statuses in which a visit has actually happened, so findings can be recorded.
#: `COMPLETED` is included: an adjuster sending a supplementary observation a week
#: after their report is normal, and refusing it would push the finding into a note.
ATTENDED_STATUSES = frozenset(
    {InspectionStatus.IN_PROGRESS, InspectionStatus.MORE_NEEDED, InspectionStatus.COMPLETED}
)


def can_transition(current: str, target: str) -> bool:
    """Whether the inspection may move from `current` to `target`.

    Unknown statuses answer `False` rather than raising, for the reason the claim's
    machine gives: a row written by an older revision is a thing to refuse to move,
    not a thing to crash on.
    """
    try:
        source = InspectionStatus(current)
        destination = InspectionStatus(target)
    except ValueError:
        return False
    return destination in _ALLOWED.get(source, frozenset())


def allowed_transitions(current: str) -> frozenset[InspectionStatus]:
    try:
        return _ALLOWED.get(InspectionStatus(current), frozenset())
    except ValueError:
        return frozenset()


def has_attended(status: str) -> bool:
    return status in ATTENDED_STATUSES


# ---------------------------------------------------------------------------
# What the visit found
# ---------------------------------------------------------------------------


class Observation(Protocol):
    """What the arithmetic needs an observation to be.

    `currency` is read by `quantified_total` and by nothing else, which is why it is
    not on this protocol: a `getattr` there keeps the three older callers — which
    pass three-field stand-ins — working unchanged.
    """

    severity: str
    quantified_minor: int | None


def quantified_minor(observations: Iterable[Observation]) -> int:
    """What the adjuster costed on site, summed.

    Only the rows that carry a figure. An observation with no amount is a finding
    the adjuster did not price — a cracked pane they noted but left to the
    contractor's quote — and treating it as zero would make a partial schedule read
    as a complete one.

    **Unaffected elements are excluded even if they carry a figure**, which they
    should not but a free-text-fed field eventually will. The sum is meant to be
    what the loss costs, and an untouched element costs nothing by definition.
    """
    return sum(
        int(observation.quantified_minor or 0)
        for observation in observations
        if observation.quantified_minor is not None
        and str(observation.severity) != DamageSeverity.UNAFFECTED
    )


def quantified_total(observations: Iterable[Observation]) -> tuple[int, str] | None:
    """What the visit costed, **with the currency it was costed in**.

    `quantified_minor` above returns a bare integer, and a bare integer is what
    caused the bug this function exists to prevent: the caller stamped the *claim's*
    booking currency onto it, so a GBP schedule on a US claim reported £180,000 as
    $180,000. Same number, different money, no warning.

    So the currency comes out of the observations and not from anywhere else.
    Returns `None` when nothing is priced, and `None` again when the priced rows do
    not agree on a currency — because there is no honest single figure in that case.
    Converting would need a rate this system does not hold, and picking one of the
    two would misreport the other. The caller shows the rows and no total, which is
    the true answer.

    A mixed schedule should not arise: `ClaimInspectionService.add_observation`
    refuses a currency the claim is not booked in. This is the second line of
    defence, and it is here rather than there because an older row could predate
    that rule.
    """
    priced = [
        observation
        for observation in observations
        if observation.quantified_minor is not None
        and str(observation.severity) != DamageSeverity.UNAFFECTED
    ]
    if not priced:
        return None

    currencies = {str(getattr(observation, "currency", "") or "") for observation in priced}
    if len(currencies) != 1:
        return None

    return sum(int(observation.quantified_minor or 0) for observation in priced), (currencies.pop())


def priced_count(observations: Iterable[Observation]) -> int:
    """How many observations carry a figure.

    Reported beside the total so a handler can see that £180,000 is the sum of two
    priced rows out of eleven, rather than the assessed cost of the whole loss.
    """
    return sum(1 for observation in observations if observation.quantified_minor is not None)


#: Severities that mean the element is a write-off or close to it. What the tab
#: leads with, because a total loss changes the reserve and a light scuff does not.
MATERIAL_SEVERITIES = frozenset({DamageSeverity.TOTAL_LOSS, DamageSeverity.SEVERE})


def material_count(observations: Iterable[Observation]) -> int:
    return sum(
        1 for observation in observations if str(observation.severity) in MATERIAL_SEVERITIES
    )


# ---------------------------------------------------------------------------
# What is still outstanding
# ---------------------------------------------------------------------------


class Action(Protocol):
    done: bool


def outstanding_actions(actions: Iterable[Action]) -> int:
    """How much is still open.

    The figure the tab's badge counts, and the reason a `completed` inspection is
    not the same as a finished one: a report can be in and three follow-ups still
    owed, which is exactly the state a handler needs surfaced rather than buried.
    """
    return sum(1 for action in actions if not action.done)


def is_settled(status: str, actions: Iterable[Action]) -> bool:
    """Whether the inspection is genuinely done with.

    Both conditions, deliberately. `COMPLETED` says the report arrived; an empty
    action list says nothing is owed. Either alone is a half-answer, and the desk
    reads this as "can I stop chasing".
    """
    return status == InspectionStatus.COMPLETED and outstanding_actions(actions) == 0


# ---------------------------------------------------------------------------
# Filing the report
# ---------------------------------------------------------------------------


class Report(Protocol):
    """What the filing rules need an inspection to be.

    `datetime` is a type, not a clock. This module still reads no `now()` — every
    instant it compares is one the caller supplied, which is what keeps the same
    inspection answering the same way twice.
    """

    status: str
    attended_at: datetime | None
    filed_at: datetime | None


def filing_blockers(
    report: Report, *, observations: Iterable[Observation], actions: Iterable[Action]
) -> list[str]:
    """What stands between the adjuster and sending this report to the handler.

    Sentences rather than codes, because the only consumer is a list a person
    reads and acts on — and a code would need a second table mapping it back to
    the sentence anyway.

    Three blockers, and each is a thing a handler would send the report back for
    if it got through:

    * **Nobody has attended.** A report on a visit that did not happen is not a
      report. This is the one that cannot be argued with.
    * **Nothing was found.** An adjuster who attended and recorded no observation
      has either not written the report up yet or is filing an empty one, and both
      are worth stopping. *Findings with no price are fine* — an unpriced schedule
      is a real answer, and insisting on money here would push adjusters into
      inventing figures, which is the opposite of what the null exists for.
    * **Actions are open.** Whatever the adjuster raised as outstanding is, by
      their own account, outstanding.

    An already-filed report is refused separately: it is not blocked, it is done.
    """
    blockers: list[str] = []

    if report.attended_at is None:
        blockers.append("The visit has not been recorded as attended.")

    if not any(True for _ in observations):
        blockers.append("Nothing has been recorded against the risk yet.")

    open_actions = outstanding_actions(actions)
    if open_actions:
        blockers.append(
            f"{open_actions} action{'s' if open_actions > 1 else ''} "
            f"{'are' if open_actions > 1 else 'is'} still open."
        )

    return blockers


def can_file(
    report: Report, *, observations: Iterable[Observation], actions: Iterable[Action]
) -> bool:
    return report.filed_at is None and not filing_blockers(
        report, observations=observations, actions=actions
    )


# ---------------------------------------------------------------------------
# The adjuster's queue
# ---------------------------------------------------------------------------

#: The chips above the adjuster's queue.
#:
#: **Two of these six are not statuses, and that is the whole point of the type.**
#: `sent_back` and `filed` are facts about the *report*; the other four are states
#: of the *visit*. An inspection that was sent back and is being worked again is
#: `in_progress` **and** sent back, and modelling the return as a fifth status
#: would make those two mutually exclusive — so the chip would empty itself the
#: moment the adjuster resumed, which is exactly when a supervisor looks for it.
CHIPS = (
    "to_do",
    "to_schedule",
    "booked_in_progress",
    "sent_back",
    "filed",
    "all",
)


class QueueRow(Protocol):
    """What the chip predicates need. Deliberately three fields and no claim.

    No reference, no claimant, no site. The chips are decided by the state of the
    inspection and nothing about the claim behind it, and a protocol that asked for
    the claim would invite a predicate that quietly started reading it.
    """

    status: str
    filed_at: datetime | None
    returned_at: datetime | None


def matches_chip(row: QueueRow, chip: str) -> bool:
    """Whether one inspection belongs under one chip.

    `to_do` is everything not with the handler — the adjuster's actual workload,
    which is why it is the queue's default. A report that was filed and sent back
    is in it again, because `filed_at` was cleared when it was returned.

    An unknown chip matches nothing rather than everything. A typo in a query
    string should empty the list, not quietly widen it to the whole book.
    """
    if chip == "all":
        return True
    if chip == "filed":
        return row.filed_at is not None
    if chip == "sent_back":
        return row.returned_at is not None
    if chip == "to_do":
        return row.filed_at is None
    if chip == "to_schedule":
        return row.filed_at is None and row.status == InspectionStatus.TO_SCHEDULE
    if chip == "booked_in_progress":
        return row.filed_at is None and row.status in {
            InspectionStatus.VISIT_BOOKED,
            InspectionStatus.IN_PROGRESS,
        }
    return False


def is_overdue(report: Report, *, report_due_at: datetime | None, now: datetime) -> bool:
    """Whether the report is late.

    Late means **the date has passed and nothing has been filed**. A report filed
    the day before it was due is not late afterwards, and an inspection with no due
    date cannot be late at all — a desk that did not set one has not been promised
    anything.

    `now` is passed in rather than read, for the reason the rest of this module
    takes no clock: the same inspection has to answer the same way twice.
    """
    if report_due_at is None or report.filed_at is not None:
        return False
    return report_due_at < now


# ---------------------------------------------------------------------------
# Whose visit is it
# ---------------------------------------------------------------------------


def owns_inspection(
    *,
    adjuster_subject: str | None,
    adjuster_email: str | None,
    subject: str | None,
    email: str | None,
) -> bool:
    """Whether this visit was instructed to this person.

    Two ways to be the same person, and the order matters. The account wins where
    there is one: `adjuster_subject` is set the first time the adjuster records
    anything, and from then on the link survives their address changing. The email
    is the fallback that makes the first time possible at all — a visit is
    instructed to a named contact long before that person signs in, and a rule that
    only understood accounts would refuse every adjuster on their first visit.

    An inspection with neither recorded belongs to nobody, and returns `False` for
    everyone. That is not a gap: it is the ordinary state of a visit instructed to a
    firm, and it means the handler records the findings — which is what happened for
    every inspection before these columns existed.

    Comparison on the address is case-folded and trimmed, because it arrives from
    two places that disagree about both: a handler typing it into the commission
    dialog, and an identity provider asserting it in a token.
    """
    if subject and adjuster_subject and subject == adjuster_subject:
        return True

    #: Only when no account has been claimed. Once `adjuster_subject` is set it is
    #: the answer, and a stale address on the row must not re-open the visit to
    #: whoever happens to hold that mailbox now.
    if adjuster_subject is None and email and adjuster_email:
        return _address(email) == _address(adjuster_email)

    return False


def may_record_findings(
    *,
    roles: Iterable[str],
    adjuster_subject: str | None,
    adjuster_email: str | None,
    subject: str | None,
    email: str | None,
) -> bool:
    """Whether this caller may record attendance, findings or actions on this visit.

    Two answers, and only one of them is about ownership.

    A caller holding any of `CLAIM_WORK_ROLES` may record on any visit, and that is
    not laxity. They commission the visit, they chase it, and on a small loss they
    write down what the adjuster told them on the telephone — the note on
    `INSPECTION_WORK_ROLES` says so. A handler restricted to visits assigned to them
    could not do the job the inspection exists to serve.

    Everybody else — in practice a loss adjuster, since the route's role gate has
    already turned away anyone who is neither — may record only on their own visit.
    Without that clause, widening the gate to admit adjusters at all would let any
    adjuster write findings onto any inspection on the desk, which is worse than the
    read-only board it replaced.

    Roles rather than capabilities here on purpose: this is a rule about what a
    persona *is* rather than a board an administrator may re-delegate, which is the
    line `require_roles` and `require_capability` already draw.
    """
    if any(role in CLAIM_WORK_ROLES for role in roles):
        return True

    return owns_inspection(
        adjuster_subject=adjuster_subject,
        adjuster_email=adjuster_email,
        subject=subject,
        email=email,
    )


def _address(value: str) -> str:
    return value.strip().casefold()


__all__ = [
    "ATTENDED_STATUSES",
    "CHIPS",
    "MATERIAL_SEVERITIES",
    "Action",
    "Observation",
    "QueueRow",
    "Report",
    "allowed_transitions",
    "can_file",
    "can_transition",
    "filing_blockers",
    "has_attended",
    "is_overdue",
    "is_settled",
    "matches_chip",
    "material_count",
    "may_record_findings",
    "outstanding_actions",
    "owns_inspection",
    "priced_count",
    "quantified_minor",
    "quantified_total",
]
