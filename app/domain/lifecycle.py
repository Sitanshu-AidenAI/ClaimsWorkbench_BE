"""The FNOL state machine.

Two rules live here and nowhere else:

* which transitions are legal (`can_transition`), so an out-of-order call is a
  409 rather than a corrupt row; and
* which status the pipeline's own findings imply (`derive_status`), so the
  officer's queue is filtered by something computed once from the exceptions
  rather than by a status each service set on its way past.

`derive_status` never moves a case out of a terminal status and never overrides a
status a human chose — a case an officer referred stays referred even if the next
document happens to clear the blocking exception.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.enums import (
    TERMINAL_FNOL_STATUSES,
    ExceptionCode,
    FNOLStatus,
)

#: Legal transitions. Absent keys are terminal.
_ALLOWED: dict[FNOLStatus, frozenset[FNOLStatus]] = {
    FNOLStatus.RECEIVED: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.AI_PROCESSING_FAILED,
            FNOLStatus.CANCELLED,
            FNOLStatus.REJECTED,
        }
    ),
    FNOLStatus.PROCESSING: frozenset(
        {
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.POSSIBLE_DUPLICATE,
            FNOLStatus.POLICY_MATCH_REQUIRED,
            FNOLStatus.READY_FOR_CLAIM,
            FNOLStatus.AI_PROCESSING_FAILED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.NEEDS_REVIEW: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.POSSIBLE_DUPLICATE,
            FNOLStatus.POLICY_MATCH_REQUIRED,
            FNOLStatus.READY_FOR_CLAIM,
            FNOLStatus.REFERRED,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.INCOMPLETE: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.POSSIBLE_DUPLICATE,
            FNOLStatus.POLICY_MATCH_REQUIRED,
            FNOLStatus.READY_FOR_CLAIM,
            FNOLStatus.REFERRED,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.POSSIBLE_DUPLICATE: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.POLICY_MATCH_REQUIRED,
            FNOLStatus.READY_FOR_CLAIM,
            FNOLStatus.REFERRED,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.POLICY_MATCH_REQUIRED: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.POSSIBLE_DUPLICATE,
            FNOLStatus.READY_FOR_CLAIM,
            FNOLStatus.REFERRED,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.AI_PROCESSING_FAILED: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.REFERRED: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.READY_FOR_CLAIM,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
    FNOLStatus.READY_FOR_CLAIM: frozenset(
        {
            FNOLStatus.PROCESSING,
            FNOLStatus.NEEDS_REVIEW,
            FNOLStatus.INCOMPLETE,
            FNOLStatus.POSSIBLE_DUPLICATE,
            FNOLStatus.POLICY_MATCH_REQUIRED,
            FNOLStatus.REFERRED,
            FNOLStatus.CLAIM_CREATED,
            FNOLStatus.REJECTED,
            FNOLStatus.CANCELLED,
        }
    ),
}

#: Exception codes that stop a claim being created at all, in the order the
#: officer should clear them. Ordered rather than a set so the blocker list the
#: API returns reads as a checklist rather than an arbitrary dump.
BLOCKING_EXCEPTIONS: tuple[ExceptionCode, ...] = (
    ExceptionCode.NO_POLICY_MATCH,
    ExceptionCode.MULTIPLE_POLICY_MATCHES,
    ExceptionCode.UNCONFIRMED_POLICY_MATCH,
    ExceptionCode.POLICY_IDENTITY_CONFLICT,
    ExceptionCode.MISSING_CRITICAL_INFORMATION,
    ExceptionCode.POSSIBLE_DUPLICATE,
    ExceptionCode.FUTURE_LOSS_DATE,
)

#: Which status an open blocking exception implies, most specific first. The
#: first match wins, so a case missing both a policy and a loss date reads as
#: "policy match required" — the thing the officer has to do first.
_STATUS_FOR_EXCEPTION: tuple[tuple[ExceptionCode, FNOLStatus], ...] = (
    # Capture before matching: a notice missing its loss date and its insured
    # cannot usefully be matched to a policy, and telling an officer to go and
    # find one would be sending them to do the second job first.
    (ExceptionCode.MISSING_CRITICAL_INFORMATION, FNOLStatus.INCOMPLETE),
    (ExceptionCode.NO_POLICY_MATCH, FNOLStatus.POLICY_MATCH_REQUIRED),
    (ExceptionCode.MULTIPLE_POLICY_MATCHES, FNOLStatus.POLICY_MATCH_REQUIRED),
    (ExceptionCode.UNCONFIRMED_POLICY_MATCH, FNOLStatus.POLICY_MATCH_REQUIRED),
    (ExceptionCode.POLICY_IDENTITY_CONFLICT, FNOLStatus.POLICY_MATCH_REQUIRED),
    (ExceptionCode.POSSIBLE_DUPLICATE, FNOLStatus.POSSIBLE_DUPLICATE),
    (ExceptionCode.FUTURE_LOSS_DATE, FNOLStatus.INCOMPLETE),
)


def is_terminal(status: FNOLStatus) -> bool:
    return status in TERMINAL_FNOL_STATUSES


def can_transition(current: FNOLStatus, target: FNOLStatus) -> bool:
    if current == target:
        return True
    return target in _ALLOWED.get(current, frozenset())


def allowed_transitions(current: FNOLStatus) -> frozenset[FNOLStatus]:
    return _ALLOWED.get(current, frozenset())


def blocking_codes(open_exception_codes: Iterable[str]) -> list[ExceptionCode]:
    """The open exceptions that stop claim creation, in checklist order."""
    open_codes = set(open_exception_codes)
    return [code for code in BLOCKING_EXCEPTIONS if code in open_codes]


def derive_status(
    current: FNOLStatus,
    *,
    open_exception_codes: Iterable[str],
    extraction_failed: bool = False,
) -> FNOLStatus:
    """The status the pipeline's findings imply, given where the case is now.

    Called at the end of the pipeline and after every officer edit, so the queue
    reflects the current findings rather than the findings that happened to be
    true when a service last ran.
    """
    if is_terminal(current) or current == FNOLStatus.REFERRED:
        return current

    if extraction_failed:
        return FNOLStatus.AI_PROCESSING_FAILED

    open_codes = set(open_exception_codes)
    for code, status in _STATUS_FOR_EXCEPTION:
        if code in open_codes:
            return status

    # Everything that remains is advisory — a CAT attribution, a severity band, a
    # fraud signal. Those are things the officer should see before creating the
    # claim, not things that stop them, so the case is ready.
    return FNOLStatus.READY_FOR_CLAIM
