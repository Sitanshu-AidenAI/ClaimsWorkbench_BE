"""Deciding whether two records describe the same loss.

The signals are the ones a claims handler would use, in the order they would use
them: the same policy on the same day is most of the answer; the same description
of the same place is the rest of it. What this module deliberately does not do is
act — the highest score it can produce still results in a candidate an officer
resolves, because two genuine claims on one policy on one day is a real thing
that happens on a bad day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core.config import FNOLSettings
from app.domain.matching import (
    location_similarity,
    name_similarity,
    normalise_reference,
    text_similarity,
    weighted_score,
)

WEIGHTS: dict[str, float] = {
    "policy": 3.0,
    "loss_date": 2.5,
    "insured": 1.5,
    "claimant": 1.0,
    "location": 2.0,
    "description": 2.0,
    "external_reference": 3.0,
    "assets": 0.8,
}

_UNCOMPARED = -1.0

#: Same-day is a full match; the score decays over a few days because a broker
#: reporting "Tuesday" and an insured reporting "the 7th" is one loss.
_DATE_DECAY_DAYS = 4


@dataclass(slots=True)
class DuplicateReason:
    signal: str
    detail: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "detail": self.detail, "score": round(self.score, 3)}


@dataclass(slots=True)
class DuplicateAssessment:
    reference: str
    kind: str
    score: float
    reasons: list[DuplicateReason] = field(default_factory=list)

    @property
    def is_candidate_at(self) -> float:
        return self.score


def compare(case: Any, other: Any, *, kind: str, reference: str) -> DuplicateAssessment:
    """Score one pair. `other` may be another notice or a created claim."""
    signals: dict[str, tuple[float, str]] = {}

    case_policy = normalise_reference(getattr(case, "policy_number", None))
    other_policy = normalise_reference(getattr(other, "policy_number", None))
    if case_policy and other_policy:
        same = case_policy == other_policy
        signals["policy"] = (1.0 if same else 0.0, f"Policy {getattr(other, 'policy_number', '')}")
    elif getattr(case, "policy_id", None) and case.policy_id == getattr(other, "policy_id", None):
        signals["policy"] = (1.0, "Matched to the same policy record")
    else:
        signals["policy"] = (_UNCOMPARED, "")

    signals["loss_date"] = _date_signal(
        getattr(case, "date_of_loss", None), getattr(other, "date_of_loss", None)
    )
    signals["insured"] = _text_signal(
        name_similarity,
        getattr(case, "insured_name", None),
        getattr(other, "insured_name", None),
        "Same insured",
    )
    signals["claimant"] = _text_signal(
        name_similarity,
        getattr(case, "claimant_name", None) or getattr(case, "insured_name", None),
        getattr(other, "claimant_name", None) or getattr(other, "insured_name", None),
        "Same claimant",
    )
    signals["location"] = _text_signal(
        location_similarity,
        getattr(case, "loss_location", None),
        getattr(other, "loss_location", None),
        "Same location",
    )
    signals["description"] = _text_signal(
        text_similarity,
        getattr(case, "loss_description", None),
        getattr(other, "loss_description", None),
        "Highly similar description of the loss",
    )
    signals["assets"] = _text_signal(
        text_similarity,
        getattr(case, "affected_assets", None),
        getattr(other, "affected_assets", None),
        "Same affected asset",
    )

    case_reference = normalise_reference(getattr(case, "external_reference", None))
    other_reference = normalise_reference(
        getattr(other, "external_reference", None) or getattr(other, "reference", None)
    )
    if case_reference and other_reference:
        same = case_reference == other_reference
        signals["external_reference"] = (
            1.0 if same else 0.0,
            "Same external / broker reference",
        )
    else:
        signals["external_reference"] = (_UNCOMPARED, "")

    score = weighted_score([(value, WEIGHTS[key]) for key, (value, _) in signals.items()])

    reasons = [
        DuplicateReason(signal=key, detail=detail, score=value)
        for key, (value, detail) in signals.items()
        if value >= 0.6 and detail
    ]
    reasons.sort(key=lambda reason: reason.score, reverse=True)

    return DuplicateAssessment(
        reference=reference, kind=kind, score=round(score, 4), reasons=reasons
    )


def is_duplicate_candidate(assessment: DuplicateAssessment, *, config: FNOLSettings) -> bool:
    return assessment.score >= config.duplicate_similarity_threshold


def _date_signal(left: datetime | None, right: datetime | None) -> tuple[float, str]:
    if left is None or right is None:
        return _UNCOMPARED, ""
    gap = abs(left - right)
    if gap <= timedelta(hours=36):
        return 1.0, f"Same date of loss ({left:%d %b %Y})"
    if gap <= timedelta(days=_DATE_DECAY_DAYS):
        return round(1.0 - gap.days / _DATE_DECAY_DAYS, 2), f"Loss dates {gap.days} days apart"
    return 0.0, ""


def _text_signal(
    comparator: Any, left: str | None, right: str | None, detail: str
) -> tuple[float, str]:
    if not left or not right:
        return _UNCOMPARED, ""
    score = float(comparator(left, right))
    return score, detail if score >= 0.6 else ""
