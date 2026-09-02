"""The manager's approval queue: escalated claims awaiting a decision.

**This is the screen that closed the loop.** A handler could refer a claim to a
manager, the claim moved to `escalated`, and the queue a manager works read
`MOCK_APPROVALS` — so the referral arrived nowhere. The handler's side and the
manager's side described unrelated worlds, which is the most expensive kind of
fixture: the one that makes a working feature look finished.

A manager is not looking at claims here, they are looking at **requests**: a figure
somebody has asked them to sign, the reason it left that desk, and how long it has
been sitting. The claim underneath is context, which is why the row carries the
handler and the waiting time rather than the policy number.

The decision itself is not here. `ClaimCaseworkService.decide` already owns every
transition and every block, and a second decision path would be a second set of
rules to keep in step — the specific failure `approval_blocks` exists to prevent.
This service reads; the existing endpoint writes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import NotFoundError
from app.domain import claim_lifecycle
from app.domain import recovery as recovery_rules
from app.domain.enums import (
    AnalysisKind,
    AuditEventType,
    NoteSection,
)
from app.models.claim import Claim
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.handler import HandlerRepository
from app.schemas import approvals as api
from app.schemas.fnol import Money, money
from app.services.claims.coverage import ClaimCoverageService
from app.services.claims.fraud_gate import fraud_gate
from app.services.fnol.audit import AuditService

#: Roughly how long a manager spends on one decision, for the clearance sentence.
#:
#: A constant rather than a measurement, and the footer says "about" because of it.
#: The real figure is the manager's own historical median, which needs a decision
#: history this system does not keep — and inventing precision would be worse than
#: admitting the approximation.
_MINUTES_PER_DECISION = 4

#: What the board is for, under the title.
_DESCRIPTION = "Escalated claims awaiting your decision — over authority limits or fraud referrals."
_DESCRIPTION_WHOLE = (
    "Every escalated claim on the book. You are not recorded against a team, so this "
    "queue cannot yet be narrowed to your own reporting line."
)


class ClaimApprovalService:
    def __init__(
        self,
        claims: ClaimRepository,
        cases: FNOLRepository,
        handlers: HandlerRepository,
        coverage: ClaimCoverageService,
        audit: AuditService | None = None,
    ) -> None:
        self._claims = claims
        self._cases = cases
        self._handlers = handlers
        self._coverage = coverage
        #: For the referral reason, which lives in the trail rather than in a column.
        #: Optional so an existing construction keeps working; absent, the sheet
        #: reports no referral, which is what it did before this was read at all.
        self._audit = audit

    # -- The queue -----------------------------------------------------------

    async def queue(
        self,
        *,
        chip: str | None = None,
        subject: str | None = None,
        whole_book: bool = True,
        now: datetime | None = None,
    ) -> api.ApprovalQueueOut:
        """Every escalated claim, under one chip, with the counts for both.

        Read once and filtered in Python rather than once per chip, for the reason
        the inspection board gives: the facet counts and the list have to describe
        the same set, and two `COUNT(*)` queries could each be true of a slightly
        different instant.

        `whole_book` is reported rather than assumed. Narrowing to a manager's own
        team needs them to be in the handler directory with a team recorded; a
        manager who is not gets the whole book and is told so, because a queue that
        silently widened while calling itself *your decision* would misstate whose
        work it is.
        """
        moment = now or datetime.now(UTC)
        rows = await self._claims.escalated_claims()

        mine: list[tuple[Claim, object | None]] = []
        team = await self._team_of(subject) if subject else None
        for claim, assignment in rows:
            if team is None or await self._on_team(assignment, team):
                mine.append((claim, assignment))

        built = [self._row(claim, assignment, now=moment) for claim, assignment in mine]

        shown = [row for row in built if _matches(row, chip)]
        facets = [
            api.ApprovalFacetOut(
                id=identifier,
                label=label,
                count=sum(1 for row in built if _matches(row, identifier)),
            )
            for identifier, label in (
                ("over_authority", "Over authority"),
                ("fraud_referral", "Fraud referral"),
            )
        ]

        return api.ApprovalQueueOut(
            items=shown,
            total=len(shown),
            metrics=await self._metrics(built, now=moment),
            facets=facets,
            description=_DESCRIPTION if team is not None else _DESCRIPTION_WHOLE,
            clearance_note=_clearance(len(shown)),
            whole_book=team is None,
        )

    def _row(self, claim: Claim, assignment: object | None, *, now: datetime) -> api.ApprovalRowOut:
        return api.ApprovalRowOut(
            id=claim.id,
            reference=claim.reference,
            claimant=claim.claimant_name or claim.insured_name or "Not recorded",
            location=claim.loss_location or "Not recorded",
            loss_type=(claim.loss_type or "unknown").replace("_", " "),
            #: "Unassigned" rather than an empty string, because an unassigned
            #: escalation is a real row and one of the conditions to be cleared.
            handler=getattr(assignment, "handler_name", None) or "Unassigned",
            waiting_days=_days_since(claim.updated_at, now),
            requested=Money(amount_minor=claim.reserve_minor or 0, currency=claim.currency),
            reason=_reason(claim),
            priority=claim.priority,
            over_authority=bool(claim.over_authority),
            fraud_flag=bool(claim.fraud_flag),
        )

    async def _metrics(
        self, rows: list[api.ApprovalRowOut], *, now: datetime
    ) -> list[api.ApprovalMetricOut]:
        """The five tiles. Four count the queue; one counts what has left it.

        `approved_mtd` reads the claims approved since the first of the month, which
        is the only tile that looks outside the queue — and the only one a manager
        uses to judge their own throughput rather than their backlog.
        """
        oldest = max((row.waiting_days for row in rows), default=0)

        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        approved = await self._claims.approved_since(start_of_month)

        #: **Each total is summed only within one currency.** The queue holds claims
        #: booked in whatever the loss was in, and the first version took the
        #: currency of the first row and stamped it on the sum — so a month whose
        #: approvals were dollars was reported in pounds. A tile that cannot be
        #: labelled truthfully says how many instead of how much, which is a smaller
        #: answer and a correct one.
        held = _total([(row.requested.amount_minor, row.requested.currency) for row in rows])
        earned = _total([(claim.reserve_minor or 0, claim.currency) for claim in approved])

        #: The desk, not the queue. A manager reading "Team 8" wants to know how many
        #: people the escalations could go back to.
        roster = await self._handlers.list_available()

        return [
            api.ApprovalMetricOut(
                id="awaiting_decision", label="Awaiting decision", display=str(len(rows))
            ),
            api.ApprovalMetricOut(
                id="total_reserve",
                label="Total reserve",
                display=_figure(held, len(rows), noun="claim"),
            ),
            api.ApprovalMetricOut(
                id="oldest_waiting",
                label="Oldest waiting",
                display=f"{oldest}d" if oldest else "—",
            ),
            api.ApprovalMetricOut(
                id="approved_mtd",
                label="Approved MTD",
                display=_figure(earned, len(approved), noun="claim"),
            ),
            api.ApprovalMetricOut(id="team", label="Team", display=str(len(roster))),
        ]

    async def _team_of(self, subject: str) -> str | None:
        handler = await self._handlers.get_by_subject(subject)
        team = getattr(handler, "team", None) if handler else None
        return team or None

    async def _on_team(self, assignment: object | None, team: str) -> bool:
        handler_id = getattr(assignment, "handler_id", None)
        if handler_id is None:
            #: An unassigned escalation belongs to nobody's team and therefore to
            #: every manager's queue. Hiding it would hide the claims that most need
            #: a decision — the ones with no handler to make it.
            return True
        handler = await self._handlers.get(handler_id)
        return bool(handler and handler.team == team)

    # -- The sheet -----------------------------------------------------------

    async def sheet(
        self, reference: str, *, subject: str | None = None, now: datetime | None = None
    ) -> api.ApprovalDetailOut:
        """One escalation, argued.

        The settlement block is the point of this payload: the figure asked for, and
        the four numbers that justify it. `recoverable` is real now — it was zero
        while recoveries were unbuilt, and a zero there meant *not tracked* rather
        than *nothing to recover*.
        """
        moment = now or datetime.now(UTC)
        claim = await self._claims.get_by_reference(reference)
        if claim is None:
            raise NotFoundError(f"No claim {reference}.")

        assignment = await self._claims.get_assignment(claim.id)
        handler = (
            await self._handlers.get(assignment.handler_id)
            if assignment and assignment.handler_id
            else None
        )
        movements = list(await self._claims.list_movements(claim.id))
        coverages = list(await self._claims.list_coverages(claim.id))
        deductibles = list(await self._claims.list_deductibles(claim.id))
        recoveries = list(await self._claims.list_recoveries(claim.id))
        notes = list(await self._claims.list_notes(claim.id))
        case = await self._cases.get(claim.fnol_case_id) if claim.fnol_case_id else None
        analysis = await self._cases.get_analysis(case.id, AnalysisKind.COVERAGE) if case else None

        gate = await fraud_gate(claim, claims=self._claims, cases=self._cases)
        blocks = claim_lifecycle.approval_blocks(
            claim,
            assignment=assignment,
            authority_limit_minor=handler.authority_limit_minor if handler else None,
            authority_currency=handler.currency if handler else None,
            incurred_minor=claim_lifecycle.incurred_minor(movements) if movements else None,
            outstanding_indicators=gate.outstanding_indicators,
            siu_open=gate.siu_open,
        )

        currency = claim.currency
        requested = claim.reserve_minor or 0
        claimed = sum(row.claimed_minor or 0 for row in coverages)
        excess = sum(row.amount_minor or 0 for row in deductibles)
        recoverable = recovery_rules.expected_minor(recoveries)

        #: The manager's own limit, so the sheet names what this is measured against.
        reader = await self._handlers.get_by_subject(subject) if subject else None

        return api.ApprovalDetailOut(
            id=claim.id,
            reference=claim.reference,
            claimant=claim.claimant_name or claim.insured_name or "Not recorded",
            status=claim.status,
            escalated_by=getattr(assignment, "handler_name", None) or "Unassigned",
            escalated_days_ago=_days_since(claim.updated_at, moment),
            settlement=api.ApprovalSettlementOut(
                requested=Money(amount_minor=requested, currency=currency),
                #: What the sections say is being claimed under the policy. Falls
                #: back to the requested figure when no section carries one, rather
                #: than showing nought against a claim that plainly claims something.
                claimed=Money(amount_minor=claimed or requested, currency=currency),
                excess=Money(amount_minor=excess, currency=currency),
                recoverable=Money(amount_minor=recoverable, currency=currency),
                #: What the book carries once the excess comes off and the recoveries
                #: land. Never below nought: a claim whose recoveries exceed its
                #: reserve is a reporting curiosity, not a negative cost.
                net_cost=Money(
                    amount_minor=max(0, requested - excess - recoverable), currency=currency
                ),
            ),
            authority_limit=money(reader.authority_limit_minor, reader.currency)
            if reader and reader.authority_limit_minor
            else None,
            conditions=_conditions(blocks),
            coverage=_coverage(analysis),
            handler_note=_handler_note(notes),
            referral=_referral(
                await self._audit.history(claim.id) if self._audit is not None else []
            ),
        )


# ---------------------------------------------------------------------------
# Reading the claim
# ---------------------------------------------------------------------------


def _matches(row: api.ApprovalRowOut, chip: str | None) -> bool:
    """Whether one row belongs under one chip. No chip means the whole scope.

    An unknown chip matches nothing rather than everything: a typo in a query string
    should empty the list, not quietly widen it.
    """
    if chip is None:
        return True
    if chip == "over_authority":
        return row.over_authority
    if chip == "fraud_referral":
        return row.fraud_flag
    return False


def _reason(claim: Claim) -> str:
    """Why the claim left the handler's desk.

    Derived rather than stored, because it is a reading of the two flags and storing
    it would let it drift from them. Both flags together is its own answer: a
    fraud-flagged claim over authority is read differently from either alone.
    """
    if claim.fraud_flag and claim.over_authority:
        return "fraud_and_authority"
    if claim.over_authority:
        return "over_authority"
    return "settlement"


def _days_since(moment: datetime | None, now: datetime) -> int:
    """Whole days, floored at nought.

    `updated_at` is the closest thing to "when it was escalated" that does not cost
    an audit query per row. It moves when the claim is touched, so a claim somebody
    has been working reads as having waited less — which understates the wait rather
    than overstating it, and is the safe direction for a queue metric.
    """
    if moment is None:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, (now - moment).days)


#: What each block means to a manager, and what they can do about it.
#:
#: The label and detail are the manager's view of the same fact the handler's bar
#: states — one function decides the blocks, and this only rephrases them.
_CONDITIONS: dict[str, tuple[str, str, tuple[tuple[str, str, bool], ...]]] = {
    "over_authority": (
        "Over the handler's authority",
        "The settlement is above what the assigned handler may release. Approving it "
        "here is the decision this queue exists for.",
        (),
    ),
    "fraud_flag": (
        "Fraud review outstanding",
        "The assessment flagged this claim and the review is not closed. Settling "
        "over an open flag is the decision most often asked about afterwards.",
        (("open_fraud_tab", "Review the indicators", False),),
    ),
    "unassigned": (
        "No handler on the file",
        "Nobody owns this claim, so there is no authority to measure the settlement "
        "against and nobody to send it back to.",
        (("assign_handler", "Assign a handler", False),),
    ),
    "decided": (
        "Already decided",
        "This claim has been closed. Nothing on this sheet can change it.",
        (),
    ),
}


def _conditions(blocks: list[claim_lifecycle.ApprovalBlock]) -> list[api.ApprovalConditionOut]:
    """The blocking conditions, and the ones that were checked and passed.

    Cleared conditions are listed too, deliberately: "the fraud review is clear" is
    worth reading before signing a large settlement, and a list that showed only
    problems would leave a manager unable to tell a checked claim from an unchecked
    one.
    """
    blocking = {block.code: block for block in blocks}
    conditions: list[api.ApprovalConditionOut] = []

    for code, (label, detail, actions) in _CONDITIONS.items():
        if code == "decided" and code not in blocking:
            #: Not a condition on a live claim — it would read as a warning that the
            #: claim might already be closed.
            continue
        conditions.append(
            api.ApprovalConditionOut(
                id=code,
                label=label,
                detail=detail if code in blocking else f"{label} — checked and clear.",
                state="blocking" if code in blocking else "cleared",
                actions=[
                    api.ApprovalActionOut(id=action, label=text, proposed=proposed)
                    for action, text, proposed in actions
                ]
                if code in blocking
                else [],
            )
        )
    return conditions


def _coverage(analysis: object | None) -> api.ApprovalCoverageOut | None:
    """The verdict the pipeline reached, as the sheet's header pill states it.

    Mapped from the analysis's own indicator rather than recomputed, so this cannot
    disagree with the coverage panel on the handler's screen.
    """
    result = getattr(analysis, "result", None) or {}
    indicator = str(result.get("indicator", "") or "")
    if not indicator:
        return None

    verdict = {
        "covered": "covered",
        "likely_covered": "covered",
        "partly_covered": "partly_covered",
        "not_covered": "not_covered",
        "likely_not_covered": "not_covered",
    }.get(indicator, "undetermined")

    return api.ApprovalCoverageOut(
        verdict=verdict,
        summary=str(result.get("reasoning", "") or "No reasoning was recorded."),
    )


def _referral(events: list[object]) -> api.ApprovalReferralOut | None:
    """The escalation that put this claim in front of a manager, from the trail.

    The **most recent** one, because a claim can be referred, returned and referred
    again, and what a manager is being asked now is the last thing that was asked.

    Read from `CLAIM_DECIDED` rather than from a dedicated column, and that is worth
    stating: the verb and the reason are already recorded there, in `context`, and a
    column beside them would be a second copy of a fact that can then disagree with
    the audit trail. The trail is the record — see `app.models.notification` on the
    same distinction.
    """
    escalations = [
        event
        for event in events
        if str(getattr(event, "event_type", "")) == str(AuditEventType.CLAIM_DECIDED)
        and str((getattr(event, "context", None) or {}).get("decision", ""))
        in {str(action) for action in claim_lifecycle.ESCALATING_DECISIONS}
    ]
    if not escalations:
        return None

    latest = max(
        escalations,
        key=lambda event: (
            getattr(event, "occurred_at", None)
            or getattr(event, "created_at", None)
            or datetime.min.replace(tzinfo=UTC)
        ),
    )
    context = getattr(latest, "context", None) or {}
    actor = str(getattr(latest, "actor", "") or "Unknown")
    reason = str(context.get("reason") or "").strip()

    return api.ApprovalReferralOut(
        decision=str(context.get("decision", "")),
        actor=actor,
        initials=_initials(actor),
        referred_at=(
            getattr(latest, "occurred_at", None)
            or getattr(latest, "created_at", None)
            or datetime.now(UTC)
        ),
        reason=reason or None,
    )


def _handler_note(notes: list[object]) -> api.ApprovalNoteOut | None:
    """The most recent note a person wrote on the claim, shown verbatim.

    Any section rather than a dedicated one: a handler explaining why they are
    escalating writes it wherever they happen to be, and a manager wants the last
    thing that was said rather than the last thing said on one tab.
    """
    written = [note for note in notes if getattr(note, "section", None) != NoteSection.GENERAL]
    latest = max(
        written or notes,
        key=lambda note: getattr(note, "created_at", None) or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    if latest is None:
        return None

    author = str(getattr(latest, "author", "") or "Unknown")
    return api.ApprovalNoteOut(
        author=author,
        initials=_initials(author),
        written_at=getattr(latest, "created_at", None) or datetime.now(UTC),
        body=str(getattr(latest, "body", "") or ""),
    )


def _initials(name: str) -> str:
    """Two letters from a name, computed here rather than sliced on the client.

    Slicing breaks on a great many names — a single word, a hyphenated surname, a
    leading initial — and a broken avatar is the kind of small wrongness that makes
    a screen feel unfinished.
    """
    parts = [part for part in name.replace("-", " ").split() if part]
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _clearance(count: int) -> str:
    if count == 0:
        return "Nothing is waiting on you."
    minutes = count * _MINUTES_PER_DECISION
    return f"Queue clears in about {minutes} minutes at four minutes a decision."


def _total(amounts: list[tuple[int, str]]) -> tuple[int, str] | None:
    """A sum with the currency it is in, or `None` when the rows disagree.

    The third time this pattern has been needed — the inspection's schedule and the
    authority check are the others — and it is the same lesson each time: adding
    minor units across currencies produces a number that is not money, and labelling
    it with whichever currency came first turns a wrong number into a convincing one.
    """
    currencies = {currency for _, currency in amounts if currency}
    if len(currencies) != 1:
        return None
    return sum(amount for amount, _ in amounts), currencies.pop()


def _figure(total: tuple[int, str] | None, count: int, *, noun: str) -> str:
    """A money tile, or a count when the money cannot be added up honestly."""
    if total is None:
        if count == 0:
            return "—"
        return f"{count} {noun}{'s' if count != 1 else ''}"
    return _compact(*total)


def _compact(amount_minor: int, currency: str) -> str:
    """A figure a tile can hold. `$3.7m`, `$259,550`."""
    symbol = {"GBP": "£", "USD": "$", "EUR": "€", "SGD": "S$"}.get(currency, "")
    major = amount_minor / 100
    if major >= 1_000_000:
        return f"{symbol}{major / 1_000_000:.1f}m".replace(".0m", "m")
    return f"{symbol}{major:,.0f}"


__all__ = ["ClaimApprovalService"]
