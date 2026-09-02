"""The six operational sections of the claim workbench, assembled from what is real.

This module's whole job is to be honest about a partly-built product, and the way
it does that is worth stating before the code:

**Nothing here invents a fact.** Where a section has no model behind it — the
inspection, the recovery register, the coverage schedule — it returns empty with
`available=False` and a sentence saying why. The temptation is to fill those with
sensible defaults so the screen looks finished; the cost of doing so is that a
demo becomes a misrepresentation and a handler learns to distrust the one section
that *is* real.

**Three sections are entirely real.** The activity log is the audit trail the
product has been writing since the first migration, grouped by subject. The
financials are the reserve ledger, the claim's own excesses with their erosion,
and the assigned handler's authority. The assessment is the coverage sections, the
parties on them, and the exposure across them — CLAWS entry categories 4, 5, 6
and 7. Every figure in all three is traceable to a row.

**One is partly real, and the split is stated per field.** The fraud review
carries the pipeline's own indicators and the loss history from other claims on
the policy; it carries no investigation record.

**All six are built.** The recovery register was the last one without a model
behind them, and they say so.

One read, not six, for the reason the workbench aggregate is one read: the
assessment cites the inspection, the financials follow the assessment and the
activity log narrates both, so assembling them from separate requests would let
them disagree about a claim that changed in between.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain import claim_lifecycle
from app.domain import coverage as coverage_rules
from app.domain import inspection as inspection_rules
from app.domain import recovery as recovery_rules
from app.domain import siu as siu_rules
from app.domain.enums import (
    ActorType,
    AnalysisKind,
    ClaimStatus,
    InspectionStatus,
    MovementType,
    NoteSection,
    SiuStatus,
)
from app.models.claim import Claim
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.handler import HandlerRepository
from app.repositories.policy import PolicyRepository
from app.schemas import claim_sections as api
from app.schemas.fnol import Money, money
from app.services.claims.coverage import ClaimCoverageService
from app.services.fnol.audit import AuditService

# ---------------------------------------------------------------------------
# Why a section is empty
# ---------------------------------------------------------------------------

#: Stated once, here, and sent to the screen rather than hard-coded in it. When a
#: section becomes real, deleting its line here is what changes the message — not
#: a string hunt through six React components.
_NO_DAMAGE_BREAKDOWN = (
    "The damage is not yet priced head by head, and what the handler is waiting "
    "on is not recorded. The coverage sections, the parties on them and the "
    "excess behind them are real."
)


class ClaimSectionsService:
    def __init__(
        self,
        claims: ClaimRepository,
        cases: FNOLRepository,
        policies: PolicyRepository,
        handlers: HandlerRepository,
        audit: AuditService,
        coverage: ClaimCoverageService,
    ) -> None:
        self._claims = claims
        self._cases = cases
        self._policies = policies
        self._handlers = handlers
        self._audit = audit
        #: Read for one thing only: the erosion of an aggregate excess, which needs
        #: other claims on the policy and therefore a query this module has no other
        #: reason to own. Everything else here reads repositories directly.
        self._coverage = coverage

    async def build(self, claim: Claim) -> api.ClaimSections:
        """Assemble every section for one claim.

        The reads are gathered up front rather than inside each section builder, so
        that the six sections are demonstrably describing the same claim at the same
        instant — and so that a reader can see, in one place, exactly how much this
        endpoint costs.
        """
        #: Read once for the whole assembly, so two sections cannot disagree about
        #: what "today" is — the limitation warnings and the overdue readings are
        #: both derived from it.
        now = datetime.now(UTC)
        case = await self._cases.get(claim.fnol_case_id) if claim.fnol_case_id else None
        policy = await self._policies.get(claim.policy_id) if claim.policy_id else None
        assignment = await self._claims.get_assignment(claim.id)
        movements = list(await self._claims.list_movements(claim.id))
        notes = list(await self._claims.list_notes(claim.id))
        coverages = list(await self._claims.list_coverages(claim.id))
        parties = list(await self._claims.list_parties(claim.id))
        links = list(await self._claims.list_coverage_links(claim.id))
        deductibles = list(await self._claims.list_deductibles(claim.id))
        inspection = await self._claims.get_inspection(claim.id)
        #: Only when there is an inspection to hang them on. Two queries that could
        #: only ever return nothing are two queries not worth making on a claim
        #: where no visit was ever commissioned, which is most of them.
        observations = (
            list(await self._claims.list_observations(inspection.id)) if inspection else []
        )
        inspection_actions = (
            list(await self._claims.list_inspection_actions(inspection.id)) if inspection else []
        )
        recoveries = list(await self._claims.list_recoveries(claim.id))
        #: Both only when there is a register to hang them on. Two queries that
        #: could only return nothing are two queries not worth making on a claim
        #: with no recoveries, which is most of them.
        recovery_events = (
            list(await self._claims.list_recovery_events(claim.id)) if recoveries else []
        )
        recovery_tasks = list(await self._claims.list_recovery_tasks(claim.id))
        siu_case = await self._claims.get_siu_case(claim.id)
        dispositions = list(await self._claims.list_fraud_dispositions(claim.id))
        #: Erosion is computed per excess rather than in the loop below, because an
        #: aggregate one queries other claims and that read must not happen inside a
        #: list comprehension a reader would take for pure mapping.
        erosions = {row.id: await self._coverage.erosion_for(claim, row) for row in deductibles}
        fraud_analysis = (
            await self._cases.get_analysis(case.id, AnalysisKind.FRAUD) if case else None
        )
        siblings = list(
            await self._claims.claims_on_policy(
                policy_id=claim.policy_id, exclude_claim_id=claim.id
            )
        )
        events = await self._audit.history(
            claim.id,
            related_ids=(claim.fnol_case_id,) if claim.fnol_case_id else (),
        )
        handler = (
            await self._handlers.get(assignment.handler_id)
            if assignment and assignment.handler_id
            else None
        )

        return api.ClaimSections(
            reference=claim.reference,
            inspection=_inspection(
                claim,
                inspection=inspection,
                observations=observations,
                actions=inspection_actions,
                notes=notes,
            ),
            assessment=_assessment(
                claim,
                notes=notes,
                movements=movements,
                coverages=coverages,
                parties=parties,
                links=links,
            ),
            financials=_financials(
                claim,
                case=case,
                policy=policy,
                movements=movements,
                handler=handler,
                coverages=coverages,
                deductibles=deductibles,
                erosions=erosions,
            ),
            fraud=_fraud(
                claim,
                analysis=fraud_analysis,
                notes=notes,
                siblings=siblings,
                siu_case=siu_case,
                dispositions=dispositions,
            ),
            recoveries=_recoveries(
                claim,
                recoveries=recoveries,
                events=recovery_events,
                tasks=recovery_tasks,
                notes=notes,
                now=now,
            ),
            activity=[_activity(event) for event in events],
        )


# ---------------------------------------------------------------------------
# Field inspection
# ---------------------------------------------------------------------------


def _inspection(
    claim: Claim,
    *,
    inspection: Any,
    observations: list[Any],
    actions: list[Any],
    notes: list[object],
) -> api.ClaimInspectionOut:
    """The visit: whether one was commissioned, and where it has got to.

    **`available` is true either way**, which is the whole change here. It used to
    be false with a sentence explaining that inspections were not recorded, and the
    screen led with that sentence. They are recorded now, so *no visit
    commissioned* is a state rather than a gap — and the tab can lead with the
    action instead of with an apology.

    The two empty-looking answers are different and the screen renders them
    differently. No row at all means nobody has instructed anybody, and the site is
    null because there is no instruction to have carried one. A row with no
    observations means the adjuster has been instructed and has not reported.
    """
    if inspection is None:
        return api.ClaimInspectionOut(
            available=True,
            unavailable_reason=None,
            next_statuses=sorted(
                str(status)
                for status in inspection_rules.allowed_transitions(
                    InspectionStatus.NOT_COMMISSIONED
                )
            ),
            status=str(InspectionStatus.NOT_COMMISSIONED),
            reference=None,
            adjuster_name=None,
            adjuster_firm=None,
            commissioned_by=None,
            commissioned_at=None,
            scheduled_at=None,
            attended_at=None,
            report_due_at=None,
            site=None,
            summary=None,
            observations=[],
            evidence=api.InspectionEvidenceOut(
                photographs=0, measurements=0, statements=0, documents=0
            ),
            actions=[],
            notes=_section_notes(notes, NoteSection.INSPECTION),
        )

    return api.ClaimInspectionOut(
        available=True,
        unavailable_reason=None,
        next_statuses=sorted(
            str(status) for status in inspection_rules.allowed_transitions(inspection.status)
        ),
        status=inspection.status,
        reference=inspection.reference,
        adjuster_name=inspection.adjuster_name,
        adjuster_firm=inspection.adjuster_firm,
        commissioned_by=inspection.commissioned_by,
        commissioned_at=inspection.commissioned_at,
        scheduled_at=inspection.scheduled_at,
        attended_at=inspection.attended_at,
        report_due_at=inspection.report_due_at,
        #: Rendered whenever anything about the site is known. The address falls
        #: back to the claim's loss location for a row written before that default
        #: existed — an adjuster with no address is the one thing this panel must
        #: not show.
        site=api.InspectionSiteOut(
            kind=inspection.site_kind,
            address=inspection.site_address or claim.loss_location,
            identifier=inspection.site_identifier,
            contact_name=inspection.site_contact_name,
            contact_phone=inspection.site_contact_phone,
            access_note=inspection.site_access_note,
        ),
        summary=inspection.summary,
        observations=[
            api.DamageObservationOut(
                id=observation.id,
                element=observation.element,
                severity=observation.severity,
                finding=observation.finding,
                #: Absent, not zero. An unpriced observation is a finding the
                #: adjuster left to a contractor's quote, and the tab sums only the
                #: rows that carry a figure.
                quantified=money(observation.quantified_minor, observation.currency)
                if observation.quantified_minor is not None and observation.currency
                else None,
                photo_count=observation.photo_count,
            )
            for observation in observations
        ],
        evidence=api.InspectionEvidenceOut(
            photographs=inspection.photographs,
            measurements=inspection.measurements,
            statements=inspection.statements,
            documents=inspection.documents,
        ),
        actions=[
            api.InspectionActionOut(
                id=action.id,
                label=action.label,
                owner=action.owner,
                due_at=action.due_at,
                done=action.done,
            )
            for action in actions
        ],
        notes=_section_notes(notes, NoteSection.INSPECTION),
    )


# ---------------------------------------------------------------------------
# Assessment — partly real
# ---------------------------------------------------------------------------

#: What the claim's own status says about where the assessment stands. Derived
#: rather than stored, because a stored recommendation and a claim status are two
#: records of one fact and they drift.
_RECOMMENDATION_FOR_STATUS: dict[str, tuple[str, str]] = {
    ClaimStatus.FNOL: (
        "hold_pending_evidence",
        "The claim has just been created and has not been assessed.",
    ),
    ClaimStatus.CLASSIFIED: (
        "hold_pending_evidence",
        "The claim has been classified and triaged but not yet assessed.",
    ),
    ClaimStatus.IN_REVIEW: (
        "hold_pending_evidence",
        "The claim is under review. No settlement position has been recorded.",
    ),
    ClaimStatus.ESCALATED: (
        "refer_to_manager",
        "The claim has been referred and is awaiting a decision above the handler.",
    ),
    ClaimStatus.APPROVED: (
        "settle",
        "The settlement was approved on this claim.",
    ),
    ClaimStatus.REJECTED: (
        "decline",
        "The claim was declined.",
    ),
}

#: The claim's four-value severity mapped onto the assessment tab's own scale.
#: They are separate vocabularies because they answer different questions — how
#: bad the loss is, versus how hard the file is — and mapping rather than reusing
#: is what keeps the two from being silently conflated.
_ASSESSED_SEVERITY: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
    "major": "critical",
}


def _assessment(
    claim: Claim,
    *,
    notes: list[object],
    movements: list[claim_lifecycle.Movement],
    coverages: list[object],
    parties: list[object],
    links: list[object],
) -> api.ClaimAssessmentOut:
    """The handler's position on the claim, and the contract it rests on.

    The coverage sections are real records now, so this section reports
    `available=True` where it used to apologise. What it still cannot supply is a
    priced breakdown of the damage and a record of what the handler is waiting on —
    `damage_heads`, `factors` and `open_questions` stay empty, and
    `unavailable_reason` names exactly those rather than the whole tab.

    `recommended_settlement` is the incurred figure from the ledger rather than the
    held reserve, and only once a settlement has actually been approved. Before
    that it is null: a settlement figure on a claim nobody has decided is a
    prediction dressed as a position.

    `exposure` is the other figure worth reading carefully. It is what the
    *responding* sections are expected to cost, each capped at its own limit —
    computed by `app.domain.coverage`, not here, so the tab and any other reader
    of that arithmetic cannot disagree.
    """
    recommendation, rationale = _RECOMMENDATION_FOR_STATUS.get(
        claim.status,
        ("hold_pending_evidence", "The claim has not been assessed."),
    )
    settled = claim.status == ClaimStatus.APPROVED
    incurred = claim_lifecycle.incurred_minor(movements) if movements else claim.reserve_minor

    by_party = {party.id: party for party in parties}
    links_by_coverage: dict[object, list[object]] = {}
    for link in links:
        links_by_coverage.setdefault(link.coverage_id, []).append(link)

    sections = [
        _coverage_section(row, links_by_coverage.get(row.id, ()), by_party, claim.currency)
        for row in coverages
    ]

    return api.ClaimAssessmentOut(
        #: True even with no sections. A claim whose notice matched no policy has no
        #: sections to select, and that is a real answer about the claim rather than
        #: a gap in the product — the same distinction the financials draw for an
        #: empty ledger.
        available=True,
        unavailable_reason=_NO_DAMAGE_BREAKDOWN,
        #: Attributed to the handler on the file rather than to whoever last saved
        #: something, and null while nobody is on it. `handler_name` is the claim's
        #: own denormalised copy, which is the name the rest of the desk shows.
        assessed_by=claim.handler_name,
        assessed_at=claim.closed_at if settled else None,
        severity=_ASSESSED_SEVERITY.get(claim.severity or "", "medium"),
        coverage_sections=sections,
        liability=api.LiabilityOut(
            position="undetermined",
            insured_share=None,
            rationale=(
                "Liability is not assessed in this system yet. A coverage standpoint "
                "is a policy question, not a liability one."
            ),
            third_party=None,
        ),
        damage_heads=[],
        recommendation=recommendation,
        recommended_settlement=(
            Money(amount_minor=incurred or 0, currency=claim.currency) if settled else None
        ),
        recommendation_rationale=rationale,
        factors=[],
        open_questions=[],
        notes=_section_notes(notes, NoteSection.ASSESSMENT),
        parties=[_party(row) for row in parties],
        exposure=Money(
            amount_minor=coverage_rules.exposure_minor(coverages), currency=claim.currency
        ),
        over_limit_sections=[row.section_key for row in coverage_rules.over_limit(coverages)],
    )


def _coverage_section(
    row: object,
    links: object,
    by_party: dict[object, object],
    currency: str,
) -> api.CoverageSectionOut:
    """One section, with the parties it concerns named rather than referenced.

    A link whose party has been deleted is skipped rather than rendered as a blank
    name. The cascade makes that unreachable today; skipping is what keeps it
    unreachable if the cascade ever changes.
    """
    named = []
    for link in links:
        party = by_party.get(link.party_id)
        if party is None:
            continue
        named.append(
            api.CoverageSectionPartyOut(
                party_id=party.id,
                name=party.name,
                role=str(party.role),
                basis=link.basis,
            )
        )

    return api.CoverageSectionOut(
        id=row.section_key,
        label=row.label,
        standpoint=str(row.standpoint),
        limit=money(row.limit_minor, row.currency or currency),
        sublimit=money(row.sublimit_minor, row.currency or currency),
        claimed=money(row.claimed_minor, row.currency or currency),
        note=row.note,
        proposed_standpoint=row.proposed_standpoint,
        source_check=row.source_check,
        overridden=bool(row.overridden),
        overridden_by=row.overridden_by,
        override_reason=row.override_reason,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        parties=named,
    )


def _party(row: object) -> api.ClaimPartyOut:
    return api.ClaimPartyOut(
        id=row.id,
        role=str(row.role),
        name=row.name,
        organisation=row.organisation,
        email=row.email,
        phone=row.phone,
        address=row.address,
        notes=row.notes,
        is_primary=bool(row.is_primary),
        source=str(row.source),
        confidence=float(row.confidence) if row.confidence is not None else None,
        fnol_party_id=row.fnol_party_id,
    )


# ---------------------------------------------------------------------------
# Financials — real
# ---------------------------------------------------------------------------


def _financials(
    claim: Claim,
    *,
    case: object | None,
    policy: object | None,
    movements: list[claim_lifecycle.Movement],
    handler: object | None,
    coverages: list[object],
    deductibles: list[object],
    erosions: dict[object, object],
) -> api.ClaimFinancialsOut:
    """The money on the file, every figure traceable to a row.

    Four sources, and it matters which is which. The **reserve lines** are the
    ledger, summed by `claim_lifecycle` rather than by this module — one definition
    of the held figure. The **excesses** are the claim's own `claim_deductibles`
    rows with their erosion, which is a change from reading the policy directly:
    the policy states what the contract says, the claim states what is being
    applied, and only the second can be corrected by a handler. The **authority
    limit** is the assigned handler's, absent when nobody is assigned. The
    **estimate** is what the notification said the loss is worth.

    The single `deductible` field is kept beside the `deductibles` list because the
    tab's headline reads one number. It is the contract-level row — the one with no
    section — and falls back to the policy's own figure only when the claim has no
    excess row at all, which happens on a claim created before this table existed.

    `available` is `True` even on a claim with no movements. An empty ledger is a
    real answer — the claim genuinely holds nothing yet — and it is the one section
    where "unbuilt" and "empty" must not be conflated, because a handler reading
    "no reserve set" needs to believe it.
    """
    currency = claim.currency
    reserves = _reserve_lines(movements, currency=currency)

    estimate_minor = getattr(case, "estimated_loss_minor", None) if case else None
    repair_minor = getattr(case, "repair_estimate_minor", None) if case else None
    estimate_total, estimate_source = _estimate(estimate_minor, repair_minor, currency)

    section_keys = {row.id: row.section_key for row in coverages}
    excesses = [
        _deductible(row, erosions.get(row.id), section_keys.get(row.coverage_id), currency)
        for row in deductibles
    ]

    #: The contract-level excess — no section against it. `next` over the list
    #: rather than a second query, because the list is already ordered nulls-first.
    contract_level = next((row for row in excesses if row.section_key is None), None)
    fallback_minor = getattr(policy, "deductible_amount_minor", None) if policy else None
    limit_minor = getattr(handler, "authority_limit_minor", None) if handler else None

    return api.ClaimFinancialsOut(
        available=True,
        unavailable_reason=None,
        currency=currency,
        deductible=(
            contract_level.amount
            if contract_level is not None
            else money(fallback_minor, getattr(policy, "currency", currency) or currency)
        ),
        #: True once any excess has actually been taken off a payment. Still false
        #: on every claim today, because there are no payments — but it is now read
        #: from the rows rather than hard-coded, so it becomes true on its own the
        #: day one carries it.
        deductible_applied=any(row.applied.amount_minor > 0 for row in excesses),
        deductibles=excesses,
        reserves=reserves,
        transactions=[_transaction(movement, currency=currency) for movement in movements],
        estimate_total=estimate_total,
        estimate_source=estimate_source,
        authority_limit=(
            Money(
                amount_minor=limit_minor,
                currency=getattr(handler, "currency", currency) or currency,
            )
            if limit_minor is not None
            else None
        ),
    )


def _deductible(
    row: object,
    erosion: object | None,
    section_key: str | None,
    currency: str,
) -> api.ClaimDeductibleOut:
    """One excess, with what is left of it.

    `erosion` is passed in rather than computed, because for an aggregate it needs
    a query across other claims on the policy and this function is pure mapping. A
    missing erosion falls back to "nothing applied anywhere", which is the correct
    reading of a row nobody has drawn against.
    """
    unit = row.currency or currency
    applied_here = getattr(erosion, "applied_here_minor", row.applied_minor)
    applied_elsewhere = getattr(erosion, "applied_elsewhere_minor", 0)
    remaining = getattr(erosion, "remaining_minor", max(0, row.amount_minor - row.applied_minor))

    return api.ClaimDeductibleOut(
        id=row.id,
        section_key=section_key,
        deductible_type=str(row.deductible_type),
        amount=Money(amount_minor=row.amount_minor, currency=unit),
        maximum_applied=money(row.maximum_applied_minor, unit),
        applied=Money(amount_minor=applied_here, currency=unit),
        applied_elsewhere=Money(amount_minor=applied_elsewhere, currency=unit),
        remaining=Money(amount_minor=remaining, currency=unit),
        exhausted=remaining == 0,
        comment=row.comment,
        set_by=row.set_by,
    )


def _reserve_lines(
    movements: list[claim_lifecycle.Movement], *, currency: str
) -> list[api.ReserveLineOut]:
    """One line per movement type that has ever moved, in a stable order.

    Ordered by the enum rather than by when each head was first touched, so the tab
    does not reorder itself as a claim develops. Heads that have never moved are
    omitted rather than shown at zero — an untouched expense reserve is not a fact
    about the claim, and four zero rows push the one real figure below the fold.
    """
    held = claim_lifecycle.held_by_movement_type(movements)
    lines: list[api.ReserveLineOut] = []

    for movement_type in MovementType:
        head = str(movement_type)
        if head not in held:
            continue
        latest = next(m for m in movements if str(m.movement_type) == head)
        lines.append(
            api.ReserveLineOut(
                head=head,
                held=Money(amount_minor=held[head], currency=currency),
                previous=Money(
                    amount_minor=claim_lifecycle.previous_held(movements, movement_type=head),
                    currency=currency,
                ),
                set_by=latest.set_by,
                set_at=latest.occurred_at,
                rationale=latest.rationale,
            )
        )
    return lines


def _transaction(
    movement: claim_lifecycle.Movement, *, currency: str
) -> api.FinancialTransactionOut:
    """One ledger row as the transactions table reads it.

    `status` is `settled` because a reserve movement takes effect when it is
    written — see `ClaimReserveMovement` on why the table has no status column.
    That is not the same as saying the money has moved, and the screen's own label
    for `settled` ("Paid") is wrong for a reserve; correcting that label is a
    frontend change this shape does not need to wait for.
    """
    return api.FinancialTransactionOut(
        id=movement.id,
        kind="reserve",
        head=str(movement.movement_type),
        description=movement.rationale,
        counterparty=None,
        amount=Money(amount_minor=movement.amount_minor, currency=movement.currency or currency),
        status="settled",
        occurred_at=movement.occurred_at,
        authorised_by=movement.set_by,
        reference=movement.basis or movement.sub_movement_type,
    )


def _estimate(
    estimated_loss_minor: int | None, repair_minor: int | None, currency: str
) -> tuple[Money | None, str | None]:
    """What the reserve was set against, and where the figure came from.

    The extracted estimate is preferred over the repair estimate when both exist:
    the first is what the notification said the loss is worth, the second is what
    one quote said one part of it costs. Naming the source is the point — an
    unattributed estimate on a financial screen is a number nobody can check.
    """
    if estimated_loss_minor is not None:
        return (
            Money(amount_minor=estimated_loss_minor, currency=currency),
            "Estimated loss, read from the notification",
        )
    if repair_minor is not None:
        return (
            Money(amount_minor=repair_minor, currency=currency),
            "Repair estimate, read from the notification",
        )
    return None, None


# ---------------------------------------------------------------------------
# Fraud & SIU — partly real
# ---------------------------------------------------------------------------


def _fraud(
    claim: Claim,
    *,
    analysis: object | None,
    notes: list[object],
    siblings: list[Claim],
    siu_case: Any,
    dispositions: list[Any],
) -> api.ClaimFraudReviewOut:
    """The fraud position: the indicators, what the desk concluded, and the case.

    The indicators are read from the stored analysis rather than recomputed, for the
    reason `_coverage` gives: the file should show what was concluded when the claim
    was created, not what today's data would conclude.

    **`siu_status` is a record now.** It used to be derived from `claim.fraud_flag`,
    which gave the screen two of its six states and made *screening* mean nothing
    more than "the model was suspicious". A case exists because somebody opened one.

    Each indicator carries the verdict recorded against it, keyed on its code. All
    four review fields null together means nobody has looked at it — the absence is
    the third state.
    """
    result = getattr(analysis, "result", None) or {}
    disposed = siu_rules.disposed_codes(dispositions)
    by_code = {str(row.code): row for row in dispositions}

    indicators: list[api.DisposedIndicatorOut] = []
    for indicator in result.get("indicators", []):
        code = str(indicator.get("code", ""))
        verdict = by_code.get(code)
        indicators.append(
            api.DisposedIndicatorOut(
                code=code,
                title=str(indicator.get("title", "")),
                detail=str(indicator.get("detail", "")),
                weight=float(indicator.get("weight", 0.0)),
                disposition=verdict.disposition if verdict else None,
                disposition_note=verdict.note if verdict else None,
                reviewed_by=verdict.reviewed_by if verdict else None,
                reviewed_at=verdict.reviewed_at if verdict else None,
            )
        )

    status = str(siu_case.status) if siu_case else str(SiuStatus.NOT_REFERRED)
    undecided = siu_rules.outstanding_indicators(
        [indicator.code for indicator in indicators], disposed
    )

    return api.ClaimFraudReviewOut(
        #: True either way now. *Not referred* is a state this system holds rather
        #: than a gap in it — the same change the inspection went through.
        available=True,
        unavailable_reason=None,
        siu_status=status,
        referred_by=siu_case.referred_by if siu_case else None,
        referred_at=siu_case.referred_at if siu_case else None,
        investigator=siu_case.investigator if siu_case else None,
        siu_reference=siu_case.siu_reference if siu_case else None,
        referral_reason=siu_case.referral_reason if siu_case else None,
        outcome=siu_case.outcome if siu_case else None,
        next_statuses=sorted(str(value) for value in siu_rules.allowed_transitions(status)),
        undecided_indicators=undecided,
        red_flags=indicators,
        #: Still empty, and the schema says why: nothing cross-checks two records
        #: against each other server-side yet.
        inconsistencies=[],
        prior_claims=[_prior(sibling) for sibling in siblings],
        recommended_actions=siu_rules.recommended_actions(
            status=status,
            outstanding=undecided,
            accepted=siu_rules.accepted_count(disposed),
            stored=siu_case.recommended_actions if siu_case else None,
        ),
        notes=_section_notes(notes, NoteSection.FRAUD),
    )


def _prior(claim: Claim) -> api.PriorClaimOut:
    """Another claim on the same policy, as a signal rather than as a claim.

    `material` is a *claim* on the reader's attention, so it is set narrowly: a
    prior loss matters here if it was itself flagged for fraud, or if it is the
    same peril as the one being looked at. Everything else is listed and not
    highlighted, because a history where every line is material is a history
    nobody reads.
    """
    settled = claim.status in {ClaimStatus.APPROVED, ClaimStatus.REJECTED}
    return api.PriorClaimOut(
        id=claim.id,
        reference=claim.reference,
        link="Same policy",
        loss_type=(claim.loss_type or "unknown").replace("_", " "),
        settled_at=claim.closed_at if settled else None,
        settled_amount=(
            Money(amount_minor=claim.paid_minor or 0, currency=claim.currency)
            if settled and claim.paid_minor
            else None
        ),
        outcome=_prior_outcome(claim),
        material=bool(claim.fraud_flag),
    )


def _prior_outcome(claim: Claim) -> str:
    if claim.status == ClaimStatus.APPROVED:
        return "Settled"
    if claim.status == ClaimStatus.REJECTED:
        return "Declined"
    return "Open"


# ---------------------------------------------------------------------------
# Recoveries — unbuilt
# ---------------------------------------------------------------------------


def _recoveries(
    claim: Claim,
    *,
    recoveries: list[Any],
    events: list[Any],
    tasks: list[Any],
    notes: list[object],
    now: datetime,
) -> api.ClaimRecoveriesOut:
    """The register: what is expected back, what has arrived, and what is owed.

    **`available` is true on every claim**, including one with an empty register.
    A claim with nothing worth pursuing and a product that does not track recoveries
    are different statements, and only the first is true now.

    The two totals are absent rather than zero when the register is empty, and
    absent again when its rows disagree on a currency — a total labelled with money
    it is not in is worse than no total, which is the lesson the inspection's
    schedule taught.
    """
    currency = recovery_rules.single_currency(recoveries)
    expected = recovery_rules.expected_minor(recoveries)
    recovered = recovery_rules.recovered_minor(recoveries)

    return api.ClaimRecoveriesOut(
        available=True,
        unavailable_reason=None,
        opportunities=[
            api.RecoveryOpportunityOut(
                id=row.id,
                kind=row.kind,
                status=row.status,
                label=row.label,
                prospects=row.prospects,
                #: What is **still** outstanding on this row, so the column can be
                #: read beside `recovered` without the two inviting a sum.
                expected=money(
                    max(0, int(row.expected_minor or 0) - int(row.recovered_minor or 0)),
                    row.currency,
                )
                or Money(amount_minor=0, currency=row.currency),
                recovered=money(row.recovered_minor or 0, row.currency)
                or Money(amount_minor=0, currency=row.currency),
                party=api.ResponsiblePartyOut(
                    name=row.party_name,
                    role=row.party_role,
                    carrier=row.party_carrier,
                    carrier_reference=row.party_carrier_reference,
                    contact=row.party_contact,
                )
                if row.party_name
                else None,
                position=row.position,
                opened_at=row.opened_at,
                limitation_at=row.limitation_at,
                next_statuses=sorted(
                    str(value) for value in recovery_rules.allowed_transitions(row.status)
                ),
            )
            for row in recoveries
        ],
        events=[
            api.RecoveryEventOut(
                id=event.id,
                recovery_id=event.recovery_id,
                occurred_at=event.occurred_at,
                description=event.description,
                actor=event.actor,
            )
            for event in events
        ],
        tasks=[
            api.RecoveryTaskOut(
                id=task.id,
                recovery_id=task.recovery_id,
                label=task.label,
                owner=task.owner,
                due_at=task.due_at,
                done=task.done,
            )
            for task in tasks
        ],
        #: Distinct from `unavailable_reason`. That says the product does not track
        #: recoveries at all; this says *this claim* has none worth pursuing, which is
        #: a handler's conclusion recorded by `ClaimRecoveryService.decline`.
        no_recovery_reason=claim.no_recovery_reason,
        #: Only a problem once the claim is decided. An empty register on a claim
        #: still in review is where every claim starts.
        consideration_overdue=(
            not recoveries
            and claim.no_recovery_reason is None
            and claim_lifecycle.is_terminal(claim.status)
        ),
        expected_total=money(expected, currency) if recoveries and currency else None,
        recovered_total=money(recovered, currency) if recoveries and currency else None,
        limitation_warnings=recovery_rules.limitation_warnings(recoveries, now=now),
        notes=_section_notes(notes, NoteSection.RECOVERY),
    )


# ---------------------------------------------------------------------------
# Activity log — real
# ---------------------------------------------------------------------------

#: How an audit actor type reads on the timeline. `AI` becomes `assistant` rather
#: than `system` deliberately: the screen distinguishes the two, and a model's
#: conclusion presented as the platform's is exactly the conflation the audit
#: trail's `actor_type` exists to prevent.
_ACTOR_KIND: dict[str, str] = {
    ActorType.HUMAN: "handler",
    ActorType.AI: "assistant",
    ActorType.SYSTEM: "system",
}


def _activity(event: object) -> api.ClaimActivityEventOut:
    """One audit row as a timeline entry.

    `detail` is the audit event's own diff rendered as a sentence, not the raw
    JSON: `before`/`after` hold only the fields that moved, so "reserve_minor
    4000000 → 6000000" is the whole story and the screen should not have to parse
    a payload to tell it.
    """
    event_type = str(getattr(event, "event_type", ""))
    return api.ClaimActivityEventOut(
        id=event.id,
        occurred_at=event.occurred_at,
        category=str(claim_lifecycle.activity_category(event_type)),
        actor=event.actor,
        actor_kind=_ACTOR_KIND.get(str(event.actor_type), "system"),
        summary=event.summary,
        detail=_diff_sentence(event),
        subject=getattr(event, "entity_reference", None),
        material=claim_lifecycle.is_material(event_type),
    )


def _diff_sentence(event: object) -> str | None:
    """`before`/`after` as one line, or nothing when there is nothing to say."""
    before = getattr(event, "before", None) or {}
    after = getattr(event, "after", None) or {}
    keys = [key for key in after if key in before and before[key] != after[key]]
    if not keys:
        return None
    return " · ".join(
        f"{key.replace('_', ' ')} {_short(before[key])} → {_short(after[key])}" for key in keys
    )


def _short(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _section_notes(notes: list[object], section: NoteSection) -> list[api.SectionNoteOut]:
    """The notes written on one tab, oldest first.

    Reversed here rather than in a second query: the repository returns newest
    first because that is what the activity log and the claim detail both want, and
    a conversation reads the other way round.
    """
    matching = [note for note in notes if str(getattr(note, "section", "")) == str(section)]
    return [
        api.SectionNoteOut(
            id=note.id,
            author=note.author,
            written_at=note.created_at,
            body=note.body,
        )
        for note in reversed(matching)
    ]


__all__ = ["ClaimSectionsService"]
