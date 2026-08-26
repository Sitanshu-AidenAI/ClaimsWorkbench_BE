"""Triage and assignment for a created claim.

Both run once, at creation, and both are re-runnable. Neither invents a person:
the assignment engine ranks handlers that exist in the handler repository, and
returns nothing rather than a plausible name when none of them qualify.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import FNOLSettings, settings
from app.core.logging import get_logger
from app.domain import triage as rules
from app.domain.assessment import text_signals
from app.domain.enums import (
    AssignmentStatus,
    ClaimStatus,
    LineOfBusiness,
    RiskLevel,
    Severity,
)
from app.domain.money import format_amount, to_currency
from app.models.claim import Claim, ClaimAssignment, ClaimTriage
from app.repositories.claim import ClaimRepository
from app.repositories.handler import HandlerRepository

logger = get_logger(__name__)


class TriageService:
    def __init__(self, claims: ClaimRepository, *, config: FNOLSettings | None = None) -> None:
        self._claims = claims
        self._config = config or settings.fnol

    async def run(self, claim: Claim, case: Any) -> ClaimTriage:
        """Classify the claim and record the route, preserving any override."""
        result = rules.triage(
            severity=_severity(claim.severity),
            estimated_loss_minor=claim.reserve_minor,
            currency=claim.currency,
            line_of_business=_line(claim.line_of_business),
            fraud_risk=_risk(case.fraud_risk),
            cat_matched=claim.cat_event_id is not None,
            injuries=case.injuries,
            fatalities=case.fatalities,
            business_interruption=bool(case.business_interruption),
            potential_litigation=bool(case.potential_litigation),
            text=text_signals(case),
            config=self._config,
        )

        existing = await self._claims.get_triage(claim.id)
        if existing is None:
            existing = ClaimTriage(claim_id=claim.id)
            self._claims.add_triage(existing)
        elif existing.overridden:
            # A manager's route stands; the recomputed categories still update, so
            # the file reflects what the claim is even where it is routed by hand.
            existing.categories = [category.value for category in result.categories]
            existing.factors = [factor.as_dict() for factor in result.factors]
            return existing

        existing.categories = [category.value for category in result.categories]
        existing.recommended_route = result.route.label
        existing.recommended_route_key = result.route.key
        existing.recommended_priority = result.priority.value
        existing.required_skill = result.route.required_skill
        existing.required_team = result.route.team
        existing.confidence = result.confidence
        existing.reasoning = result.reasoning
        existing.factors = [factor.as_dict() for factor in result.factors]

        claim.priority = result.priority.value
        claim.status = ClaimStatus.CLASSIFIED
        claim.fraud_flag = rules.TriageCategory.FRAUD_REVIEW in result.categories

        logger.info(
            "claim_triaged",
            reference=claim.reference,
            route=result.route.key,
            priority=result.priority.value,
            categories=[category.value for category in result.categories],
        )
        return existing

    async def override(
        self,
        claim: Claim,
        *,
        route_key: str,
        route_label: str,
        priority: str,
        reason: str,
        actor: str,
    ) -> ClaimTriage | None:
        triage_row = await self._claims.get_triage(claim.id)
        if triage_row is None:
            return None

        if not triage_row.overridden:
            triage_row.original_route = triage_row.recommended_route

        triage_row.recommended_route = route_label
        triage_row.recommended_route_key = route_key
        triage_row.recommended_priority = priority
        triage_row.overridden = True
        triage_row.overridden_by = actor
        triage_row.override_reason = reason
        claim.priority = priority
        return triage_row


class AssignmentService:
    """Recommends, and records, who owns a claim.

    The engine's output is a recommendation until somebody accepts it. That is not
    a limitation of the implementation — it is the workflow: a claims manager owns
    allocation, and a queue with a recommendation against it is a legitimate
    resting place for a claim.
    """

    def __init__(
        self,
        handlers: HandlerRepository,
        claims: ClaimRepository,
        *,
        config: FNOLSettings | None = None,
    ) -> None:
        self._handlers = handlers
        self._claims = claims
        #: Only for the exchange rates the authority check needs — a reserve in
        #: dollars against an authority limit in pounds is not a comparison.
        self._config = config or settings.fnol

    async def recommend(self, claim: Claim, triage_row: ClaimTriage) -> ClaimAssignment:
        available = list(await self._handlers.list_available())
        recommendation = rules.recommend_assignment(
            available,
            triage_result=_as_triage_result(triage_row),
            severity=_severity(claim.severity),
            line_of_business=_line(claim.line_of_business),
            country=claim.loss_country,
        )

        assignment = await self._claims.get_assignment(claim.id)
        if assignment is None:
            assignment = ClaimAssignment(claim_id=claim.id)
            self._claims.add_assignment(assignment)
        elif assignment.status == AssignmentStatus.ASSIGNED:
            return assignment  # Already owned by a person; do not re-recommend.

        assignment.team = recommendation.team
        assignment.queue = recommendation.queue
        assignment.strategy = recommendation.strategy.value
        assignment.confidence = recommendation.confidence
        assignment.reasoning = recommendation.reasoning
        assignment.alternatives = [candidate.as_dict() for candidate in recommendation.alternatives]

        if recommendation.handler is not None:
            assignment.handler_id = recommendation.handler.handler_id
            assignment.handler_name = recommendation.handler.name
            assignment.status = AssignmentStatus.RECOMMENDED
        else:
            assignment.handler_id = None
            assignment.handler_name = None
            assignment.status = AssignmentStatus.UNASSIGNED

        logger.info(
            "claim_assignment_recommended",
            reference=claim.reference,
            handler=assignment.handler_name,
            queue=assignment.queue,
            status=assignment.status,
        )
        return assignment

    async def assign(
        self, claim: Claim, *, handler_id: uuid.UUID | None, actor: str
    ) -> ClaimAssignment | None:
        """Accept the recommendation, or put a named handler on the claim."""
        assignment = await self._claims.get_assignment(claim.id)
        if assignment is None:
            return None

        target_id = handler_id or assignment.handler_id
        if target_id is None:
            return None

        handler = await self._handlers.get(target_id)
        if handler is None:
            return None

        previous = assignment.handler_id
        assignment.handler_id = handler.id
        assignment.handler_name = handler.full_name
        assignment.team = handler.team
        assignment.status = AssignmentStatus.ASSIGNED
        assignment.assigned_by = actor
        assignment.assigned_at = datetime.now(UTC)
        if handler_id is not None and handler_id != previous:
            assignment.strategy = rules.AssignmentStrategy.MANUAL.value

        claim.handler_name = handler.full_name
        claim.status = ClaimStatus.IN_REVIEW
        await self._handlers.increment_workload(handler.id, 1)

        if claim.reserve_minor and handler.authority_limit_minor:
            # The reserve is in the claim's currency and the limit in the handler's.
            # Comparing the integers put a $600,000 reserve inside a £500,000
            # authority; converting first is the whole of the fix.
            reserve = to_currency(
                claim.reserve_minor, claim.currency, handler.currency, config=self._config
            )
            if reserve is None:
                # Not comparable, so not cleared. Flagging sends the claim to the
                # over-authority queue for a manager, which is the safe side of a
                # comparison nobody can make.
                claim.over_authority = True
                logger.warning(
                    "claim_authority_not_comparable",
                    reference=claim.reference,
                    reserve=format_amount(claim.reserve_minor, claim.currency),
                    authority=format_amount(handler.authority_limit_minor, handler.currency),
                )
            else:
                claim.over_authority = reserve.amount_minor > handler.authority_limit_minor

        logger.info(
            "claim_assigned", reference=claim.reference, handler=handler.full_name, actor=actor
        )
        return assignment


def _as_triage_result(row: ClaimTriage) -> rules.TriageResult:
    """Rebuild the pure result from its stored form, so scoring reads one type."""
    from app.domain.rules import RouteDefinition

    categories = []
    for value in row.categories or []:
        try:
            categories.append(rules.TriageCategory(value))
        except ValueError:
            continue

    return rules.TriageResult(
        categories=categories,
        route=RouteDefinition(
            key=row.recommended_route_key,
            label=row.recommended_route,
            team=row.required_team or row.recommended_route,
            required_skill=row.required_skill,
        ),
        priority=rules.Priority(row.recommended_priority),
        confidence=float(row.confidence or 0.0),
    )


def _severity(value: str | None) -> Severity | None:
    try:
        return Severity(value) if value else None
    except ValueError:
        return None


def _line(value: str | None) -> LineOfBusiness | None:
    try:
        return LineOfBusiness(value) if value else None
    except ValueError:
        return None


def _risk(value: str | None) -> RiskLevel | None:
    try:
        return RiskLevel(value) if value else None
    except ValueError:
        return None
