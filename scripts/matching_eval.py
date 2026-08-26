"""Scoring the policy matcher against the answer key that ships with the fixtures.

`case_data/_ground_truth/MATCHING_GROUND_TRUTH.json` states, for all 24 packs, which
of the twelve policies in `policy/policy-book.json` each notice should resolve to and
how confidently. It has been generated from the scenario definitions since the packs
were built and read by nothing at all — not a test, not a script — so a change to a
weight, a comparator or the ladder could move every one of those 24 answers and no
check in the repository would notice.

This module is the missing consumer, and it sits beside the generator that wrote the
answer key rather than inside `app/`: it reads two files off the checkout, which is not
something a domain module should do. It is pure — no database, no model, no network —
because everything it exercises is: `app.domain.policy_identification` compares a
`NoticeSignals` against a list of `PolicyFacts` in Python, and both sides can be
built from the two JSON files on disk. That makes the eval cheap enough to run in the
unit suite, which is the only place a regression gets caught before it ships.

What it is not: an extraction eval. It feeds the matcher the field *values* the answer
key states, so it measures the matcher and holds extraction constant at perfect. That
is the right seam to measure first — a matching failure on correct inputs is a bug in
the matcher, and one on extracted inputs could be either.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import FNOLSettings
from app.domain import policy_identification as engine
from app.domain.enums import LineOfBusiness, PolicyConfidence

#: The answer key's own vocabulary for a confidence band, and what it means here.
#: `NO_MATCH` is the important one: four packs are built to resolve to nothing, and a
#: matcher that answers them confidently is worse than one that answers them wrongly.
CONFIDENCE_WORDS: dict[str, PolicyConfidence | None] = {
    "Exact": PolicyConfidence.EXACT,
    "Strong": PolicyConfidence.STRONG,
    "Possible": PolicyConfidence.POSSIBLE,
    "Weak": PolicyConfidence.WEAK,
    "No Match": None,
}

#: How the answer key writes a line of business, and the enum it means. The key is
#: written for a person to read — "Commercial property", "Builders risk" — and the
#: matcher's `line_of_business` signal compares enum values.
LINE_WORDS: dict[str, LineOfBusiness] = {
    "commercial property": LineOfBusiness.PROPERTY,
    "property": LineOfBusiness.PROPERTY,
    "builders risk": LineOfBusiness.CONSTRUCTION,
    "builders risk / course of construction": LineOfBusiness.CONSTRUCTION,
    "course of construction": LineOfBusiness.CONSTRUCTION,
    "contractors general liability": LineOfBusiness.LIABILITY,
    "general liability": LineOfBusiness.LIABILITY,
    "contractors equipment": LineOfBusiness.ENGINEERING,
    "contractors equipment / installation floater": LineOfBusiness.ENGINEERING,
    "installation floater": LineOfBusiness.ENGINEERING,
    "commercial auto": LineOfBusiness.MOTOR,
    "workers compensation": LineOfBusiness.WORKERS_COMPENSATION,
    "casualty": LineOfBusiness.CASUALTY,
}

#: How the answer key spells "this notice resolves to nothing". Four packs do, and
#: they are the ones worth having: a matcher that binds a policy to a notice whose
#: insured is not in the book is doing more damage than one that finds nothing.
NO_MATCH = "NO_MATCH"

_MONEY_RE = re.compile(r"([A-Z]{3})?\s*\$?\s*([\d,]+(?:\.\d{1,2})?)")
_POSTCODE_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def repo_root() -> Path:
    """The checkout, found from this file rather than from the caller's cwd.

    Scripts in this repository resolve `.env` relative to the cwd and the ground
    truth must not join them: an eval that silently reads no packs and reports 100%
    is worse than one that fails to start.
    """
    return Path(__file__).resolve().parents[1]


def ground_truth_path() -> Path:
    return repo_root() / "case_data" / "_ground_truth" / "MATCHING_GROUND_TRUTH.json"


def policy_book_path() -> Path:
    return repo_root() / "policy" / "policy-book.json"


# ---------------------------------------------------------------------------
# Loading the two files
# ---------------------------------------------------------------------------


def load_ground_truth(path: Path | None = None) -> list[dict[str, Any]]:
    rows = json.loads((path or ground_truth_path()).read_text("utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path or ground_truth_path()} holds no packs")
    return rows


def load_policy_book(path: Path | None = None) -> list[engine.PolicyFacts]:
    """The book as the matcher compares it, straight off the JSON.

    `policy_id` is the policy number, because there is no database here and the
    number is the only stable identity the file carries. Every assertion downstream
    reads back a number, so this is the identity that makes the eval legible.
    """
    rows = json.loads((path or policy_book_path()).read_text("utf-8"))
    return [_facts_from_json(row) for row in rows]


def _facts_from_json(row: dict[str, Any]) -> engine.PolicyFacts:
    locations = tuple(
        engine.PolicyLocationFacts(
            location_ref=entry[0],
            description=entry[1],
            address=entry[2],
            postcode=entry[3],
            sum_insured_minor=entry[4],
            deductible_minor=entry[5],
            is_primary=bool(entry[6]),
        )
        for entry in row.get("locations_scheduled") or ()
        if len(entry) >= 7 and entry[2]
    )
    return engine.PolicyFacts(
        policy_id=row["policy_number"],
        policy_number=row["policy_number"],
        insured_name=row["insured_name"],
        line_of_business=row.get("line_of_business") or "",
        status=row.get("status") or "active",
        effective_date=date.fromisoformat(row["effective_date"]),
        expiry_date=date.fromisoformat(row["expiry_date"]),
        insured_organisation=row.get("insured_organisation"),
        insured_email=row.get("insured_email"),
        insured_domain=row.get("insured_domain"),
        broker_name=row.get("broker_name"),
        broker_reference=row.get("broker_reference"),
        broker_domain=row.get("broker_domain"),
        insurer_name=row.get("insurer_name"),
        policy_type=row.get("policy_type"),
        primary_location=row.get("primary_location"),
        site_address=row.get("site_address"),
        locations=locations,
        project_name=row.get("project_name"),
        project_reference=row.get("project_reference"),
        contract_number=row.get("contract_number"),
        principal_name=row.get("principal_name"),
        contractor_name=row.get("contractor_name"),
        currency=row.get("currency") or "USD",
        limit_amount_minor=row.get("limit_amount_minor"),
        deductible_amount_minor=row.get("deductible_amount_minor"),
        perils_covered=tuple(row.get("perils_covered") or ()),
        exclusions=tuple(row.get("exclusions") or ()),
    )


# ---------------------------------------------------------------------------
# One pack as a notice
# ---------------------------------------------------------------------------


def _signal(value: str | None, field_key: str | None = None) -> engine.SignalValue | None:
    text = (value or "").strip()
    return engine.SignalValue(value=text, field_key=field_key) if text else None


def _parse_day(value: str | None) -> date | None:
    """The answer key writes "10 January 2026"; nothing else has to be handled."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d %B %Y").date()
    except ValueError:
        return None


def _parse_money_minor(value: str | None) -> int | None:
    if not value:
        return None
    match = _MONEY_RE.search(value)
    if match is None:
        return None
    return round(float(match.group(2).replace(",", "")) * 100)


def notice_from_row(row: dict[str, Any]) -> engine.NoticeSignals:
    """One answer-key row as the signals the matcher would have been given.

    The values the notice *states* only. `expected_matched_policy_number` is the
    answer and is deliberately not fed in — the failure mode of an eval like this is
    leaking the answer into the input, and here that would be a single typo away.
    """
    location = row.get("loss_location") or ""
    postcode = _POSTCODE_RE.search(location)
    line = LINE_WORDS.get((row.get("line_of_business") or "").strip().lower())
    loss_day = _parse_day(row.get("date_of_loss"))

    return engine.NoticeSignals(
        policy_number=_signal(row.get("mentioned_policy_number"), "policy.policy_number"),
        broker_reference=_signal(row.get("broker_reference"), "policy.broker_reference"),
        insured_name=_signal(row.get("insured_name"), "policy.insured_name"),
        insured_organisation=_signal(row.get("insured_name"), "policy.insured_organisation"),
        broker_domain=_signal(row.get("broker_domain")),
        loss_location=_signal(location, "loss.loss_location"),
        loss_postcode=_signal(postcode.group(1) if postcode else None, "loss.loss_postcode"),
        date_of_loss=_signal(row.get("date_of_loss"), "loss.date_of_loss"),
        line_of_business=_signal(line.value if line else None),
        cause_of_loss=_signal(row.get("cause_of_loss"), "loss.cause_of_loss"),
        estimated_loss_minor=_parse_money_minor(row.get("estimated_loss")),
        loss_day=loss_day,
    )


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PackOutcome:
    """What the matcher said about one pack, against what it should have said."""

    pack: str
    expected_policy_number: str | None
    expected_confidence: PolicyConfidence | None
    matched_policy_number: str | None
    matched_confidence: PolicyConfidence | None
    recommended_policy_number: str | None
    score: float
    status: str
    candidates: int

    @property
    def top_correct(self) -> bool:
        """Did the top candidate name the right policy — or nothing, when nothing is right?"""
        return self.matched_policy_number == self.expected_policy_number

    @property
    def recommendation_correct(self) -> bool:
        """Did the engine recommend what the answer key says it should?

        Which is not always "the expected policy". The key states a confidence
        alongside the answer, and a `Possible` pack is one the engine is *supposed*
        to hand to a person: recommending nothing there is the right behaviour, and
        scoring it as a miss would push the metric towards a matcher that binds
        ambiguous notices. So:

        * `NO_MATCH` — right only when nothing is recommended;
        * `Exact` or `Strong` — right only when the expected policy is recommended;
        * `Possible` or `Weak` — right when the expected policy is recommended *or*
          nothing is, and wrong when some other policy is.

        The last clause is the one doing the work. "Recommended nothing" and
        "recommended the wrong policy" are not the same failure, and only one of
        them puts a claim on somebody else's contract.
        """
        if self.expected_policy_number is None:
            return self.recommended_policy_number is None
        if self.expected_confidence in (PolicyConfidence.EXACT, PolicyConfidence.STRONG):
            return self.recommended_policy_number == self.expected_policy_number
        return self.recommended_policy_number in (None, self.expected_policy_number)

    @property
    def confidence_correct(self) -> bool:
        return self.matched_confidence == self.expected_confidence


@dataclass(slots=True)
class EvalReport:
    outcomes: list[PackOutcome] = field(default_factory=list)

    @property
    def packs(self) -> int:
        return len(self.outcomes)

    @property
    def top_correct(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.top_correct)

    @property
    def recommendation_correct(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.recommendation_correct)

    @property
    def confidence_correct(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.confidence_correct)

    def rate(self, correct: int) -> float:
        return correct / self.packs if self.packs else 0.0

    def failures(self) -> list[PackOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.recommendation_correct]


def evaluate(
    *,
    config: FNOLSettings | None = None,
    ground_truth: list[dict[str, Any]] | None = None,
    policies: list[engine.PolicyFacts] | None = None,
) -> EvalReport:
    """Run every pack in the answer key through the matcher and score the answers."""
    settings = config or FNOLSettings()
    rows = ground_truth if ground_truth is not None else load_ground_truth()
    book = policies if policies is not None else load_policy_book()

    report = EvalReport()
    for row in rows:
        expected_raw = (row.get("expected_matched_policy_number") or "").strip()
        # The key spells "no policy" as a sentinel string rather than as null.
        expected_number = None if expected_raw in ("", NO_MATCH) else expected_raw
        expected_word = (row.get("match_confidence") or "").strip()
        if expected_word not in CONFIDENCE_WORDS:
            raise ValueError(
                f"{row.get('pack')}: unknown confidence {expected_word!r} in the answer key"
            )

        result = engine.identify(notice_from_row(row), book, config=settings)
        best = result.best
        report.outcomes.append(
            PackOutcome(
                pack=str(row.get("pack")),
                expected_policy_number=expected_number,
                expected_confidence=CONFIDENCE_WORDS[expected_word],
                matched_policy_number=str(best.policy_id) if best is not None else None,
                matched_confidence=best.confidence if best is not None else None,
                recommended_policy_number=(
                    str(result.recommended_policy_id)
                    if result.recommended_policy_id is not None
                    else None
                ),
                score=round(best.score, 4) if best is not None else 0.0,
                status=result.status.value,
                candidates=len(result.candidates),
            )
        )
    return report


__all__ = [
    "CONFIDENCE_WORDS",
    "LINE_WORDS",
    "NO_MATCH",
    "EvalReport",
    "PackOutcome",
    "evaluate",
    "ground_truth_path",
    "load_ground_truth",
    "load_policy_book",
    "notice_from_row",
    "policy_book_path",
    "repo_root",
]
