"""The SIU case: referral, investigation, and what the desk concluded per indicator.

The fifth pure-rules module, same shape as the others. No session, no clock, no ORM
beyond attribute reads.

Two things here are worth reading before the code.

**A referral is a person's act, and the flag is a machine's.** `claims.fraud_flag`
is set by the pipeline's own scoring; an SIU case exists because a handler decided
to refer. The tab used to derive `siu_status` from the flag, which gave it two of
six states and made "screening" mean nothing more than "the model was suspicious".
The two are now separate records and the status is only ever moved by somebody.

**A disposition is per indicator, and its absence is a state.** Each fraud
indicator can be `accepted` or `discounted` by a reviewer, and an indicator with no
disposition row has not been looked at. Three states from two values plus absence,
rather than a nullable third value nobody can define — which is why `undecided` is
not in the enum.

The status machine allows a closed case to be reopened. Investigations are reopened
when new information arrives, and forcing a second case for the same suspicion
would split one story across two records.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.domain.enums import CLOSED_SIU_STATUSES, FraudDisposition, SiuStatus

# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

#: Legal transitions. Absent keys are terminal.
#:
#: `SCREENING → NOT_REFERRED` is the case that matters most and is easy to miss: a
#: handler screens a suspicion, decides there is nothing in it, and the claim goes
#: back to being ordinary. Without that edge the only way out of screening would be
#: to refer it, which would make every glance at a claim an accusation.
_ALLOWED: dict[SiuStatus, frozenset[SiuStatus]] = {
    SiuStatus.NOT_REFERRED: frozenset({SiuStatus.SCREENING, SiuStatus.REFERRED}),
    SiuStatus.SCREENING: frozenset({SiuStatus.REFERRED, SiuStatus.NOT_REFERRED}),
    SiuStatus.REFERRED: frozenset(
        {
            SiuStatus.UNDER_INVESTIGATION,
            SiuStatus.CLOSED_NO_ACTION,
            SiuStatus.CLOSED_CONFIRMED,
        }
    ),
    SiuStatus.UNDER_INVESTIGATION: frozenset(
        {SiuStatus.CLOSED_NO_ACTION, SiuStatus.CLOSED_CONFIRMED}
    ),
    #: Reopening. New information arrives after a case is closed more often than
    #: anybody would like, and a second case for the same suspicion splits one
    #: story in half.
    SiuStatus.CLOSED_NO_ACTION: frozenset({SiuStatus.UNDER_INVESTIGATION}),
    SiuStatus.CLOSED_CONFIRMED: frozenset({SiuStatus.UNDER_INVESTIGATION}),
}

#: The statuses that mean somebody outside the handler's desk owns this now.
WITH_SIU = frozenset({SiuStatus.REFERRED, SiuStatus.UNDER_INVESTIGATION})


def can_transition(current: str, target: str) -> bool:
    try:
        source = SiuStatus(current)
        destination = SiuStatus(target)
    except ValueError:
        return False
    return destination in _ALLOWED.get(source, frozenset())


def allowed_transitions(current: str) -> frozenset[SiuStatus]:
    try:
        return _ALLOWED.get(SiuStatus(current), frozenset())
    except ValueError:
        return frozenset()


def is_closed(status: str) -> bool:
    return status in CLOSED_SIU_STATUSES


def is_with_siu(status: str) -> bool:
    return status in WITH_SIU


def requires_investigator(status: str) -> bool:
    """Whether this status is meaningless without somebody's name against it.

    An investigation nobody owns is not an investigation, so moving a case to
    `under_investigation` needs an investigator. Referring does not: a referral sits
    in SIU's own queue before anybody picks it up, and demanding a name at that
    point would have handlers inventing one.
    """
    return status == SiuStatus.UNDER_INVESTIGATION


# ---------------------------------------------------------------------------
# Dispositions
# ---------------------------------------------------------------------------


class Disposition(Protocol):
    code: str
    disposition: str


def disposed_codes(dispositions: Iterable[Disposition]) -> dict[str, str]:
    """Indicator code to what was concluded about it. Later rows win.

    The repository returns one row per code, so a collision here means two verdicts
    on one indicator — which the unique constraint forbids. Taking the last is the
    harmless reading if it ever happens.
    """
    return {str(row.code): str(row.disposition) for row in dispositions}


def outstanding_indicators(codes: Iterable[str], disposed: dict[str, str]) -> int:
    """How many indicators nobody has decided about.

    The figure the tab's badge counts, and the honest replacement for a count of
    conflicts that was always nought.
    """
    return sum(1 for code in codes if code not in disposed)


def accepted_count(disposed: dict[str, str]) -> int:
    return sum(1 for value in disposed.values() if value == FraudDisposition.ACCEPTED)


def recommended_actions(
    *, status: str, outstanding: int, accepted: int, stored: Iterable[str] | None = None
) -> list[str]:
    """What to do next, in the desk's words.

    `stored` wins whenever SIU has written its own actions — an investigator's
    instructions beat a generated list every time. What follows is the fallback, and
    it is procedure rather than analysis, which is why it is phrased the same way
    every time.

    The order matters: the thing that unblocks the claim comes first. An accepted
    indicator on an unreferred claim is the strongest signal on this tab and the
    sentence that should be read first.
    """
    written = [line for line in (stored or []) if line.strip()]
    if written:
        return written

    actions: list[str] = []
    if accepted and status == SiuStatus.NOT_REFERRED:
        actions.append(
            f"{accepted} indicator{'s' if accepted > 1 else ''} "
            f"{'have' if accepted > 1 else 'has'} been accepted and this claim has not "
            "been referred. Refer it, or record why not."
        )
    if outstanding:
        actions.append(
            f"Review the {outstanding} indicator{'s' if outstanding > 1 else ''} nobody "
            "has decided about, against the source document."
        )
    if is_with_siu(status):
        actions.append("The case is with SIU. Chase the investigator before settling.")
    if not actions:
        actions.append("Nothing outstanding on the fraud review.")
    return actions


__all__ = [
    "WITH_SIU",
    "Disposition",
    "accepted_count",
    "allowed_transitions",
    "can_transition",
    "disposed_codes",
    "is_closed",
    "is_with_siu",
    "outstanding_indicators",
    "recommended_actions",
    "requires_investigator",
]
