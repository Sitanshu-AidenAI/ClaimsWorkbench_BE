"""Scoring a notification against candidate policies.

The rule an officer would apply, written down: a policy number that matches is
nearly decisive; a name, a broker and a location that all agree are collectively
about as good; and a policy that was not in force on the date of loss is a
candidate to *show*, not a candidate to bind. Nothing here selects a policy — it
ranks, and a human confirms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.config import FNOLSettings
from app.domain.enums import PolicyMatchStrength
from app.domain.matching import (
    location_similarity,
    name_similarity,
    normalise,
    normalise_reference,
    weighted_score,
)

#: How much each signal contributes. The policy number dominates because it is
#: the only identifier that is supposed to be unique.
WEIGHTS: dict[str, float] = {
    "policy_number": 5.0,
    "insured_name": 2.0,
    "organisation": 1.2,
    "broker": 0.8,
    "email": 1.0,
    "location": 0.8,
    "line_of_business": 0.6,
    "in_force": 1.5,
}

#: Score reserved for an exact, character-for-character policy number match on a
#: policy that was also in force. Anything less has to be earned across signals.
_UNCOMPARED = -1.0


@dataclass(slots=True)
class PolicyCandidate:
    policy_id: Any
    policy_number: str
    score: float
    strength: PolicyMatchStrength
    matched_on: dict[str, float] = field(default_factory=dict)
    reasoning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "score": round(self.score, 4),
            "match_strength": self.strength.value,
            "matched_on": {key: round(value, 3) for key, value in self.matched_on.items()},
            "reasoning": self.reasoning,
        }


def score_policy(case: Any, policy: Any, *, config: FNOLSettings) -> PolicyCandidate:
    """How well one policy answers this notification."""
    signals: dict[str, float] = {}

    case_number = normalise_reference(getattr(case, "policy_number", None))
    policy_number = normalise_reference(policy.policy_number)
    if case_number and policy_number:
        if case_number == policy_number:
            signals["policy_number"] = 1.0
        elif case_number in policy_number or policy_number in case_number:
            signals["policy_number"] = 0.8
        else:
            signals["policy_number"] = 0.0
    else:
        signals["policy_number"] = _UNCOMPARED

    signals["insured_name"] = _compare(
        name_similarity, getattr(case, "insured_name", None), policy.insured_name
    )
    signals["organisation"] = _compare(
        name_similarity,
        getattr(case, "insured_organisation", None),
        policy.insured_organisation,
    )
    signals["broker"] = _compare(
        name_similarity, getattr(case, "reporter_organisation", None), policy.broker_name
    )

    reporter_email = normalise(getattr(case, "reporter_email", None))
    policy_email = normalise(policy.insured_email)
    if reporter_email and policy_email:
        signals["email"] = (
            1.0 if reporter_email == policy_email else _domain_match(reporter_email, policy_email)
        )
    else:
        signals["email"] = _UNCOMPARED

    signals["location"] = _compare(
        location_similarity, getattr(case, "loss_location", None), policy.primary_location
    )

    case_line = getattr(case, "line_of_business", None)
    if case_line and policy.line_of_business:
        signals["line_of_business"] = 1.0 if case_line == policy.line_of_business else 0.0
    else:
        signals["line_of_business"] = _UNCOMPARED

    in_force = _in_force(getattr(case, "date_of_loss", None), policy)
    signals["in_force"] = _UNCOMPARED if in_force is None else (1.0 if in_force else 0.0)

    score = weighted_score([(value, WEIGHTS[key]) for key, value in signals.items()])
    strength = _strength(score, signals, config)

    return PolicyCandidate(
        policy_id=policy.id,
        policy_number=policy.policy_number,
        score=round(score, 4),
        strength=strength,
        matched_on={key: value for key, value in signals.items() if value >= 0.0},
        reasoning=_explain(signals, in_force),
    )


#: Signals that identify *this* insured rather than merely describing the claim.
#: A candidate scored on line of business and dates alone is not a match — every
#: property policy on the book would qualify — so at least one of these has to
#: have been comparable.
IDENTIFYING_SIGNALS = ("policy_number", "insured_name", "organisation", "broker", "email")


def rank_candidates(
    case: Any, policies: list[Any], *, config: FNOLSettings, limit: int = 5
) -> list[PolicyCandidate]:
    """The candidates worth showing, best first."""
    scored = [score_policy(case, policy, config=config) for policy in policies]
    scored = [candidate for candidate in scored if _identified(candidate)]
    viable = [
        candidate
        for candidate in scored
        if candidate.score >= config.policy_match_candidate_threshold
    ]
    viable.sort(key=lambda candidate: candidate.score, reverse=True)
    return viable[:limit]


def overall_strength(
    candidates: list[PolicyCandidate], *, config: FNOLSettings
) -> PolicyMatchStrength:
    """What the candidate list as a whole means for the officer.

    Two candidates that both score highly is `POSSIBLE`, not `HIGH`: the point of
    the distinction is whether a human has a choice to make, and two strong
    matches is precisely when they do.
    """
    if not candidates:
        return PolicyMatchStrength.NONE

    best = candidates[0]
    contenders = [
        candidate
        for candidate in candidates[1:]
        if candidate.score >= config.policy_match_high_threshold
    ]
    if contenders:
        return PolicyMatchStrength.POSSIBLE
    return best.strength


def _identified(candidate: PolicyCandidate) -> bool:
    return any(candidate.matched_on.get(signal, 0.0) > 0.0 for signal in IDENTIFYING_SIGNALS)


def _strength(score: float, signals: dict[str, float], config: FNOLSettings) -> PolicyMatchStrength:
    exact_number = signals.get("policy_number", _UNCOMPARED) >= 1.0
    in_force = signals.get("in_force", _UNCOMPARED)

    if exact_number and in_force != 0.0 and score >= config.policy_match_high_threshold:
        return PolicyMatchStrength.EXACT
    if score >= config.policy_match_high_threshold:
        return PolicyMatchStrength.HIGH
    if score >= config.policy_match_candidate_threshold:
        return PolicyMatchStrength.POSSIBLE
    return PolicyMatchStrength.NONE


def _compare(comparator: Any, left: str | None, right: str | None) -> float:
    if not left or not right:
        return _UNCOMPARED
    return float(comparator(left, right))


def _domain_match(left: str, right: str) -> float:
    """Same organisation, different mailbox — worth something, not everything."""
    left_domain = left.rsplit("@", 1)[-1]
    right_domain = right.rsplit("@", 1)[-1]
    return 0.6 if left_domain and left_domain == right_domain else 0.0


def _in_force(loss_date: datetime | date | None, policy: Any) -> bool | None:
    if loss_date is None:
        return None
    day = loss_date.date() if isinstance(loss_date, datetime) else loss_date
    return bool(policy.effective_date <= day <= policy.expiry_date)


def _explain(signals: dict[str, float], in_force: bool | None) -> str:
    reasons: list[str] = []
    if signals.get("policy_number", _UNCOMPARED) >= 1.0:
        reasons.append("the policy number matches exactly")
    elif signals.get("policy_number", _UNCOMPARED) >= 0.8:
        reasons.append("the policy number is a close match")
    if signals.get("insured_name", _UNCOMPARED) >= 0.8:
        reasons.append("the insured name matches")
    if signals.get("organisation", _UNCOMPARED) >= 0.8:
        reasons.append("the insured organisation matches")
    if signals.get("broker", _UNCOMPARED) >= 0.7:
        reasons.append("the broker matches")
    if signals.get("email", _UNCOMPARED) >= 0.6:
        reasons.append("the contact email matches the policy record")
    if signals.get("location", _UNCOMPARED) >= 0.5:
        reasons.append("the loss location matches the insured location")

    if in_force is True:
        reasons.append("the policy was in force on the date of loss")
    elif in_force is False:
        reasons.append("the policy was NOT in force on the date of loss")

    if not reasons:
        return "No strong signals matched; ranked on partial similarity alone."
    return f"Matched because {', '.join(reasons)}."
