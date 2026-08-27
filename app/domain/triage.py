"""Where a new claim should go, and who should own it.

Triage decides *what kind of thing* the claim is; assignment decides *who*. They
are separate because the answers change independently — a major loss is still a
major loss when the specialist who handles them is on leave, and the route should
not silently become "whoever is free".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import FNOLSettings
from app.domain.assessment import has_litigation_signal, has_serious_injury_signal
from app.domain.enums import (
    AssignmentStrategy,
    LineOfBusiness,
    Priority,
    RiskLevel,
    Severity,
    TriageCategory,
)
from app.domain.money import format_amount, to_base
from app.domain.rules import ROUTES, SPECIALIST_LINES, RouteDefinition


@dataclass(slots=True)
class TriageFactor:
    factor: str
    detail: str
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {"factor": self.factor, "detail": self.detail, "weight": round(self.weight, 3)}


@dataclass(slots=True)
class TriageResult:
    categories: list[TriageCategory]
    route: RouteDefinition
    priority: Priority
    confidence: float
    factors: list[TriageFactor] = field(default_factory=list)

    @property
    def reasoning(self) -> str:
        if not self.factors:
            return "Routed on the default path: nothing on the claim required specialist handling."
        return " ".join(factor.detail for factor in self.factors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "categories": [category.value for category in self.categories],
            "route": self.route.label,
            "route_key": self.route.key,
            "team": self.route.team,
            "required_skill": self.route.required_skill,
            "priority": self.priority.value,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "factors": [factor.as_dict() for factor in self.factors],
        }


def triage(
    *,
    severity: Severity | None,
    estimated_loss_minor: int | None,
    currency: str | None = None,
    line_of_business: LineOfBusiness | None,
    fraud_risk: RiskLevel | None,
    cat_matched: bool,
    injuries: int | None,
    fatalities: int | None,
    business_interruption: bool,
    potential_litigation: bool,
    text: str,
    config: FNOLSettings,
) -> TriageResult:
    """Classify the claim and pick its route.

    A claim can carry several categories at once; the route is chosen by the most
    specialised one, because a major loss with a fraud signal goes to SIU first
    and the major-loss desk second, not to both.

    `currency` is what the exposure is denominated in. The major-loss threshold is
    an amount in `config.base_currency`, so the exposure is converted before it is
    read against it — and when no rate connects them the money rule does not fire
    at all, because routing a $1,150,000 trench collapse to the fast track on the
    strength of an unconverted integer is worse than routing it as complex.
    """
    categories: list[TriageCategory] = []
    factors: list[TriageFactor] = []
    exposure_currency = currency or config.base_currency
    exposure = to_base(estimated_loss_minor, exposure_currency, config=config)
    exposure_not_comparable = estimated_loss_minor is not None and exposure is None

    if fraud_risk in (RiskLevel.HIGH, RiskLevel.MEDIUM):
        categories.append(TriageCategory.FRAUD_REVIEW)
        factors.append(
            TriageFactor(
                "fraud_indicators",
                f"Fraud indicators are {fraud_risk.value}, so the file is referred for review.",
                0.9 if fraud_risk is RiskLevel.HIGH else 0.6,
            )
        )

    if potential_litigation or has_litigation_signal(text):
        categories.append(TriageCategory.LITIGATION_RISK)
        factors.append(
            TriageFactor(
                "litigation",
                "Legal correspondence or representation is indicated in the notification.",
                0.8,
            )
        )

    if exposure_not_comparable:
        factors.append(
            TriageFactor(
                "exposure_not_comparable",
                f"The exposure of {format_amount(estimated_loss_minor, exposure_currency)} was "
                f"not weighed against the major-loss threshold: no rate converts "
                f"{exposure_currency} into {config.base_currency}. Route by hand.",
                0.0,
            )
        )

    if exposure and exposure.amount_minor >= config.major_loss_threshold_minor:
        categories.append(TriageCategory.MAJOR_LOSS)
        factors.append(
            TriageFactor(
                "exposure",
                f"Estimated exposure of {exposure.describe()} exceeds the "
                f"{format_amount(config.major_loss_threshold_minor, config.base_currency)} "
                "major-loss threshold.",
                0.9,
            )
        )
    elif severity is Severity.CRITICAL:
        categories.append(TriageCategory.MAJOR_LOSS)
        factors.append(
            TriageFactor("severity", "The initial severity assessment is critical.", 0.8)
        )

    if fatalities or has_serious_injury_signal(text):
        if TriageCategory.MAJOR_LOSS not in categories:
            categories.append(TriageCategory.MAJOR_LOSS)
        factors.append(
            TriageFactor("serious_injury", "Serious injury or loss of life is reported.", 1.0)
        )

    if cat_matched:
        categories.append(TriageCategory.CAT_CLAIM)
        factors.append(
            TriageFactor("catastrophe", "The loss is attributed to a catastrophe event.", 0.7)
        )

    if line_of_business in SPECIALIST_LINES:
        categories.append(TriageCategory.SPECIALIST_REQUIRED)
        factors.append(
            TriageFactor(
                "specialist_line",
                f"{line_of_business.value.replace('_', ' ').title()} claims are handled by "
                "the specialist desk.",
                0.6,
            )
        )

    if not categories:
        complex_signals = bool(
            business_interruption
            or (injuries or 0) > 0
            or severity in (Severity.HIGH,)
            # An exposure nobody could compare is not evidence of a small claim.
            or exposure_not_comparable
        )
        if complex_signals:
            categories.append(TriageCategory.COMPLEX)
            factors.append(
                TriageFactor(
                    "complexity",
                    "Injuries, business interruption or an exposure that could not be "
                    "banded make this more than a fast-track claim.",
                    0.5,
                )
            )
        else:
            categories.append(TriageCategory.SIMPLE)
            factors.append(
                TriageFactor(
                    "straightforward",
                    "No severity, fraud, litigation or catastrophe signals were raised.",
                    0.5,
                )
            )

    route = _route_for(categories)
    priority = _priority(severity, categories)
    # Confidence is how strongly the deciding factors fired, not a guess: a claim
    # routed on one weak signal should say so.
    confidence = min(0.95, 0.4 + 0.5 * max((factor.weight for factor in factors), default=0.0))

    return TriageResult(
        categories=_dedupe(categories),
        route=route,
        priority=priority,
        confidence=round(confidence, 2),
        factors=factors,
    )


def _route_for(categories: list[TriageCategory]) -> RouteDefinition:
    present = set(categories)
    for required, route in ROUTES:
        if required & present:
            return route
    return ROUTES[-1][1]


def _priority(severity: Severity | None, categories: list[TriageCategory]) -> Priority:
    present = set(categories)
    if severity is Severity.CRITICAL or TriageCategory.MAJOR_LOSS in present:
        return Priority.URGENT
    if severity is Severity.HIGH or present & {
        TriageCategory.FRAUD_REVIEW,
        TriageCategory.LITIGATION_RISK,
        TriageCategory.CAT_CLAIM,
    }:
        return Priority.HIGH
    if severity is Severity.MEDIUM or TriageCategory.COMPLEX in present:
        return Priority.STANDARD
    return Priority.ROUTINE


def _dedupe(categories: list[TriageCategory]) -> list[TriageCategory]:
    seen: set[TriageCategory] = set()
    ordered: list[TriageCategory] = []
    for category in categories:
        if category not in seen:
            seen.add(category)
            ordered.append(category)
    return ordered


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HandlerScore:
    handler_id: Any
    name: str
    team: str
    score: float
    reasons: list[str]
    blockers: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "handler_id": str(self.handler_id),
            "handler_name": self.name,
            "team": self.team,
            "score": round(self.score, 4),
            "reasons": self.reasons,
            "blockers": self.blockers,
        }


@dataclass(slots=True)
class AssignmentRecommendation:
    handler: HandlerScore | None
    alternatives: list[HandlerScore]
    strategy: AssignmentStrategy
    queue: str
    team: str
    confidence: float
    reasoning: str


#: What each dimension is worth. Skill and severity authority dominate because
#: they are the two that make an assignment wrong rather than merely suboptimal.
ASSIGNMENT_WEIGHTS = {
    "skill": 3.0,
    "team": 2.0,
    "line_of_business": 1.5,
    "country": 1.0,
    "severity_authority": 2.5,
    "capacity": 1.5,
}

_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


def recommend_assignment(
    handlers: list[Any],
    *,
    triage_result: TriageResult,
    severity: Severity | None,
    line_of_business: LineOfBusiness | None,
    country: str | None,
) -> AssignmentRecommendation:
    """Rank handlers for a triaged claim.

    Returns a recommendation, never an assignment. When nobody qualifies — no
    handler holds the skill, or everyone is over capacity — it returns no handler
    and names the queue instead, which is a claims manager's decision to make and
    not something to paper over by picking the least-bad option silently.
    """
    scored = [
        _score_handler(
            handler,
            triage_result=triage_result,
            severity=severity,
            line_of_business=line_of_business,
            country=country,
        )
        for handler in handlers
    ]
    eligible = [candidate for candidate in scored if not candidate.blockers]
    eligible.sort(key=lambda candidate: candidate.score, reverse=True)

    queue = f"{triage_result.route.team} queue"

    if not eligible:
        blocked = sorted(scored, key=lambda candidate: candidate.score, reverse=True)[:3]
        return AssignmentRecommendation(
            handler=None,
            alternatives=blocked,
            strategy=AssignmentStrategy.QUEUE,
            queue=queue,
            team=triage_result.route.team,
            confidence=0.0,
            reasoning=(
                f"No available handler holds the authority or skill this claim needs, so it "
                f"is queued to {queue} for a claims manager to allocate."
            ),
        )

    best = eligible[0]
    runner_up = eligible[1].score if len(eligible) > 1 else 0.0
    confidence = min(0.95, 0.5 + (best.score - runner_up))

    return AssignmentRecommendation(
        handler=best,
        alternatives=eligible[1:4],
        strategy=(
            AssignmentStrategy.SKILL_BASED
            if triage_result.route.required_skill
            else AssignmentStrategy.WORKLOAD
        ),
        queue=queue,
        team=triage_result.route.team,
        confidence=round(confidence, 2),
        reasoning=f"{best.name} — {'; '.join(best.reasons)}.",
    )


def _score_handler(
    handler: Any,
    *,
    triage_result: TriageResult,
    severity: Severity | None,
    line_of_business: LineOfBusiness | None,
    country: str | None,
) -> HandlerScore:
    reasons: list[str] = []
    blockers: list[str] = []
    parts: list[tuple[float, float]] = []

    required_skill = triage_result.route.required_skill
    skills = {str(skill).lower() for skill in (handler.skills or [])}
    if required_skill:
        if required_skill in skills:
            reasons.append(f"holds the {required_skill.replace('_', ' ')} skill")
            parts.append((1.0, ASSIGNMENT_WEIGHTS["skill"]))
        else:
            blockers.append(f"does not hold the {required_skill.replace('_', ' ')} skill")
            parts.append((0.0, ASSIGNMENT_WEIGHTS["skill"]))

    if handler.team == triage_result.route.team:
        reasons.append(f"is on the {handler.team} team")
        parts.append((1.0, ASSIGNMENT_WEIGHTS["team"]))
    else:
        parts.append((0.2, ASSIGNMENT_WEIGHTS["team"]))

    lines = {str(line).lower() for line in (handler.lines_of_business or [])}
    if line_of_business and lines:
        if line_of_business.value in lines:
            reasons.append(f"works {line_of_business.value.replace('_', ' ')} claims")
            parts.append((1.0, ASSIGNMENT_WEIGHTS["line_of_business"]))
        else:
            parts.append((0.1, ASSIGNMENT_WEIGHTS["line_of_business"]))

    countries = {str(entry).lower() for entry in (handler.countries or [])}
    if country and countries:
        parts.append((1.0 if country.lower() in countries else 0.2, ASSIGNMENT_WEIGHTS["country"]))
        if country.lower() in countries:
            reasons.append(f"covers {country}")

    if severity is not None:
        try:
            authority = _SEVERITY_RANK[Severity(handler.max_severity)]
        except (ValueError, KeyError):
            authority = 1
        if authority >= _SEVERITY_RANK[severity]:
            parts.append((1.0, ASSIGNMENT_WEIGHTS["severity_authority"]))
        else:
            blockers.append(f"is authorised only to {handler.max_severity} severity claims")
            parts.append((0.0, ASSIGNMENT_WEIGHTS["severity_authority"]))

    capacity = max(1, int(handler.capacity or 1))
    load = int(handler.open_claims or 0)
    headroom = max(0.0, 1.0 - load / capacity)
    parts.append((headroom, ASSIGNMENT_WEIGHTS["capacity"]))
    if load >= capacity:
        blockers.append(f"is at capacity ({load} of {capacity} open claims)")
    elif headroom >= 0.4:
        reasons.append(f"has capacity ({load} of {capacity} open)")

    total_weight = sum(weight for _, weight in parts) or 1.0
    score = sum(value * weight for value, weight in parts) / total_weight

    if not reasons:
        reasons.append("is available")

    return HandlerScore(
        handler_id=handler.id,
        name=handler.full_name,
        team=handler.team,
        score=round(score, 4),
        reasons=reasons,
        blockers=blockers,
    )
