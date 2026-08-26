"""The recovery register's arithmetic and its state machine.

The fourth of these modules — `lifecycle.py` for the notice, `claim_lifecycle.py`
for the claim, `inspection.py` for the visit, this for money coming back — and
deliberately the same shape. Pure functions over values: no session, no ORM beyond
attribute reads, no clock.

Three rules live here and nowhere else.

**What is expected back, and what has actually arrived, are two figures.** They are
never added and never conflated: `expected_minor` is a judgement about the future
and `recovered_minor` is a bank statement. A register that reported one number
would be reporting a forecast as an asset, which is the specific way a recovery
ledger flatters a loss ratio.

**A recovery does not net off the reserve.** `claim_lifecycle.incurred_minor`
already excludes recovery movements, and this module gives the reason a second
home: the reserve is what the claim is expected to cost, and money coming back is
tracked *against* it. Netting an identified-but-unbanked recovery into the reserve
would shrink the figure a handler is measured against on the strength of a lawyer's
opinion.

**Limitation is a date, not a status.** A subrogation barred by limitation is still
`pursuing` on the record until somebody writes it off, because the desk's own view
and the legal position are different facts and only one of them is a decision
anybody took.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Protocol

from app.domain.enums import CLOSED_RECOVERY_STATUSES, RecoveryStatus

# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

#: Legal transitions. Absent keys are terminal.
#:
#: `IN_NEGOTIATION → PURSUING` exists because negotiations collapse and the file
#: goes back to being chased. `RECOVERED` is terminal and `WRITTEN_OFF` is not:
#: a written-off subrogation that the third party's insurer suddenly engages with
#: is a real and welcome event, and forcing the desk to open a second recovery for
#: the same money would double-count the expectation.
_ALLOWED: dict[RecoveryStatus, frozenset[RecoveryStatus]] = {
    RecoveryStatus.IDENTIFIED: frozenset({RecoveryStatus.PURSUING, RecoveryStatus.WRITTEN_OFF}),
    RecoveryStatus.PURSUING: frozenset(
        {
            RecoveryStatus.IN_NEGOTIATION,
            RecoveryStatus.RECOVERED,
            RecoveryStatus.WRITTEN_OFF,
        }
    ),
    RecoveryStatus.IN_NEGOTIATION: frozenset(
        {
            RecoveryStatus.PURSUING,
            RecoveryStatus.RECOVERED,
            RecoveryStatus.WRITTEN_OFF,
        }
    ),
    RecoveryStatus.WRITTEN_OFF: frozenset({RecoveryStatus.PURSUING}),
}


def can_transition(current: str, target: str) -> bool:
    """Whether a recovery may move from `current` to `target`.

    Unknown statuses answer `False` rather than raising, for the reason the other
    three machines give: a row written by an older revision is a thing to refuse to
    move, not a thing to crash on.
    """
    try:
        source = RecoveryStatus(current)
        destination = RecoveryStatus(target)
    except ValueError:
        return False
    return destination in _ALLOWED.get(source, frozenset())


def allowed_transitions(current: str) -> frozenset[RecoveryStatus]:
    try:
        return _ALLOWED.get(RecoveryStatus(current), frozenset())
    except ValueError:
        return frozenset()


def is_closed(status: str) -> bool:
    return status in CLOSED_RECOVERY_STATUSES


# ---------------------------------------------------------------------------
# The money
# ---------------------------------------------------------------------------


class Recovery(Protocol):
    """What the arithmetic needs a recovery to be."""

    status: str
    expected_minor: int
    recovered_minor: int
    currency: str


def _live(recoveries: Iterable[Recovery]) -> list[Recovery]:
    """The ones still worth expecting something from.

    Written-off recoveries are excluded from the expectation and **not** from what
    has been recovered: money that came back before the rest was written off is
    still money that came back.
    """
    return [
        recovery for recovery in recoveries if str(recovery.status) != RecoveryStatus.WRITTEN_OFF
    ]


def expected_minor(recoveries: Iterable[Recovery]) -> int:
    """What the desk still expects back.

    Excludes what has already arrived, so this is an *outstanding* expectation
    rather than a gross one. A recovery expected at £40,000 with £30,000 banked has
    £10,000 outstanding, and reporting £40,000 beside the £30,000 would let a reader
    add them to £70,000 — which is the whole reason these two are separate figures.

    Never negative: a recovery that brought back more than expected has nothing
    outstanding, not a negative expectation.
    """
    return sum(
        max(0, int(recovery.expected_minor or 0) - int(recovery.recovered_minor or 0))
        for recovery in _live(recoveries)
    )


def recovered_minor(recoveries: Iterable[Recovery]) -> int:
    """What has actually come back. A bank statement, not a forecast."""
    return sum(int(recovery.recovered_minor or 0) for recovery in recoveries)


def single_currency(recoveries: Iterable[Recovery]) -> str | None:
    """The currency the register is in, or `None` when the rows disagree.

    The same rule `inspection.quantified_total` follows, learned the same way: a
    total labelled with a currency it is not in is worse than no total. The service
    refuses a recovery in a currency the claim is not booked in, so this is the
    second line of defence for rows written before that rule.
    """
    currencies = {str(getattr(recovery, "currency", "") or "") for recovery in recoveries}
    currencies.discard("")
    return currencies.pop() if len(currencies) == 1 else None


def open_count(recoveries: Iterable[Recovery]) -> int:
    return sum(1 for recovery in recoveries if not is_closed(str(recovery.status)))


# ---------------------------------------------------------------------------
# Limitation
# ---------------------------------------------------------------------------


class Limited(Protocol):
    status: str
    limitation_at: datetime | None


def limitation_warnings(
    recoveries: Iterable[Limited], *, now: datetime, within_days: int = 90
) -> list[str]:
    """Recoveries whose limitation date is close or gone, as sentences.

    The one thing on this tab that costs real money by being missed: a subrogation
    not issued before limitation is a recovery that stops existing, and nobody gets
    a reminder from the courts.

    Closed recoveries are skipped — a recovered or written-off file has nothing left
    to be barred. `now` is passed in, for the reason the rest of this module takes
    no clock.
    """
    warnings: list[str] = []
    for recovery in recoveries:
        limitation = recovery.limitation_at
        if limitation is None or is_closed(str(recovery.status)):
            continue
        days = (limitation.date() - now.date()).days
        label = getattr(recovery, "label", "This recovery")
        if days < 0:
            warnings.append(f"{label} passed its limitation date {abs(days)} days ago.")
        elif days <= within_days:
            warnings.append(f"{label} is barred in {days} day{'s' if days != 1 else ''}.")
    return warnings


def prospects_band(prospects: float | None) -> str:
    """A probability as a word, because a handler reads a word.

    `None` is *not assessed* rather than nought. A recovery nobody has judged and
    one judged hopeless are different facts, and 0% would state the second.
    """
    if prospects is None:
        return "not assessed"
    if prospects >= 0.7:
        return "strong"
    if prospects >= 0.4:
        return "even"
    if prospects > 0.0:
        return "weak"
    return "none"


def barred(limitation_at: datetime | None, *, on: date) -> bool:
    return limitation_at is not None and limitation_at.date() < on


__all__ = [
    "Limited",
    "Recovery",
    "allowed_transitions",
    "barred",
    "can_transition",
    "expected_minor",
    "is_closed",
    "limitation_warnings",
    "open_count",
    "prospects_band",
    "recovered_minor",
    "single_currency",
]
