"""Completeness, severity, fraud signals and coverage indicators.

All four are deterministic functions over a case. None of them calls a language
model, and that is not a limitation — it is the point. "Is the estimated loss
above the major-loss threshold" is arithmetic; asking a model to do arithmetic
buys a slower, more expensive answer that cannot be reproduced or audited. The
model's job is reading the notification; deciding what the notification *means*
belongs here, where the rule is visible and the test is cheap.

Every result carries its factors, because an officer who is shown "High severity"
without the reasons has been given a verdict rather than an assessment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.core.config import FNOLSettings
from app.domain.enums import CoverageIndicator, LineOfBusiness, RiskLevel, Severity
from app.domain.matching import normalise, tokens
from app.domain.rules import (
    INJURY_SIGNALS,
    LITIGATION_SIGNALS,
    RequiredField,
    required_fields,
)

# ---------------------------------------------------------------------------
# Reading a case
# ---------------------------------------------------------------------------

#: Field path -> how to read it off the case. One table, used by the completeness
#: engine, the review API and the field-provenance writer, so a path that appears
#: on screen is a path that can actually be read and edited.
FIELD_READERS: dict[str, str] = {
    "notification.reporter_name": "reporter_name",
    "notification.reporter_organisation": "reporter_organisation",
    "notification.reporter_role": "reporter_role",
    "notification.reporter_email": "reporter_email",
    "notification.reporter_phone": "reporter_phone",
    "policy.policy_number": "policy_number",
    "policy.insured_name": "insured_name",
    "policy.insured_organisation": "insured_organisation",
    "policy.policy_type": "policy_type",
    "loss.date_of_loss": "date_of_loss",
    "loss.loss_location": "loss_location",
    "loss.loss_country": "loss_country",
    "loss.loss_description": "loss_description",
    "loss.cause_of_loss": "cause_of_loss",
    "loss.affected_assets": "affected_assets",
    "loss.injuries": "injuries",
    "loss.fatalities": "fatalities",
    "financial.estimated_loss": "estimated_loss_minor",
    "financial.repair_estimate": "repair_estimate_minor",
    "additional.police_reference": "police_reference",
    "additional.incident_reference": "incident_reference",
    "additional.authorities_involved": "authorities_involved",
}

#: Paths whose presence is answered by something other than a column.
DERIVED_PATHS = frozenset(
    {"documents.supporting", "parties.claimant_name", "parties.third_parties", "parties.witnesses"}
)


def read_field(case: Any, path: str) -> Any:
    """The current value behind a field path, or `None`."""
    attribute = FIELD_READERS.get(path)
    return getattr(case, attribute, None) if attribute else None


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FieldStatus:
    path: str
    label: str
    section: str
    critical: bool
    #: `present`, `missing`, `uncertain` or `conflicting`.
    state: str
    detail: str | None = None


@dataclass(slots=True)
class CompletenessResult:
    score: float
    present: list[FieldStatus] = field(default_factory=list)
    missing: list[FieldStatus] = field(default_factory=list)
    uncertain: list[FieldStatus] = field(default_factory=list)
    conflicting: list[FieldStatus] = field(default_factory=list)

    @property
    def missing_critical(self) -> list[FieldStatus]:
        return [status for status in self.missing if status.critical]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "present": [_status_dict(status) for status in self.present],
            "missing": [_status_dict(status) for status in self.missing],
            "uncertain": [_status_dict(status) for status in self.uncertain],
            "conflicting": [_status_dict(status) for status in self.conflicting],
        }


def _status_dict(status: FieldStatus) -> dict[str, Any]:
    return {
        "path": status.path,
        "label": status.label,
        "section": status.section,
        "critical": status.critical,
        "state": status.state,
        "detail": status.detail,
    }


#: A critical field counts for this much more than an optional one, so a notice
#: missing only its police reference still reads as substantially complete.
CRITICAL_WEIGHT = 3.0
OPTIONAL_WEIGHT = 1.0
#: A value the extractor was unsure of is worth part of a field, not none of it.
UNCERTAIN_CREDIT = 0.5


def assess_completeness(
    case: Any,
    *,
    document_count: int,
    party_roles: set[str],
    field_confidences: dict[str, float | None],
    config: FNOLSettings,
    low_confidence_threshold: float | None = None,
) -> CompletenessResult:
    """How much of what this claim type needs is actually here.

    The percentage is a weighted count of configured fields, not a made-up figure:
    every point it reports can be traced to a named field in `app.domain.rules`,
    and the officer is shown the list rather than the number alone.
    """
    threshold = (
        low_confidence_threshold
        if low_confidence_threshold is not None
        else config.low_confidence_threshold
    )
    line = _line_of_business(case)
    result = CompletenessResult(score=0.0)

    earned = 0.0
    available = 0.0

    for requirement in required_fields(line):
        weight = CRITICAL_WEIGHT if requirement.critical else OPTIONAL_WEIGHT
        available += weight

        present, detail = _is_present(case, requirement, document_count, party_roles)
        status = FieldStatus(
            path=requirement.path,
            label=requirement.label,
            section=requirement.section,
            critical=requirement.critical,
            state="present" if present else "missing",
            detail=detail,
        )

        if not present:
            result.missing.append(status)
            continue

        confidence = field_confidences.get(requirement.path)
        if confidence is not None and confidence < threshold:
            status.state = "uncertain"
            status.detail = f"Read with {confidence:.0%} confidence — worth checking."
            result.uncertain.append(status)
            earned += weight * UNCERTAIN_CREDIT
        else:
            result.present.append(status)
            earned += weight

    for conflict in _conflicts(case):
        result.conflicting.append(conflict)

    result.score = round(earned / available, 4) if available else 0.0
    return result


def _is_present(
    case: Any, requirement: RequiredField, document_count: int, party_roles: set[str]
) -> tuple[bool, str | None]:
    if requirement.path == "documents.supporting":
        return document_count > 0, f"{document_count} attached" if document_count else None
    if requirement.path == "parties.claimant_name":
        has_party = bool({"claimant", "insured"} & party_roles)
        return has_party or bool(getattr(case, "insured_name", None)), None
    if requirement.path == "parties.third_parties":
        return "third_party" in party_roles, None
    if requirement.path == "parties.witnesses":
        return "witness" in party_roles, None

    value = read_field(case, requirement.path)
    if value is None:
        return False, None
    if isinstance(value, str):
        return bool(value.strip()), None
    return True, None


def _conflicts(case: Any) -> list[FieldStatus]:
    """Facts on the case that cannot both be true.

    Distinct from missing information: a notice stating a loss after the policy
    expired is not incomplete, it is inconsistent, and the officer resolves the
    two differently.
    """
    conflicts: list[FieldStatus] = []
    loss_date = getattr(case, "date_of_loss", None)
    received = getattr(case, "received_at", None)

    if loss_date and received and loss_date > received:
        conflicts.append(
            FieldStatus(
                path="loss.date_of_loss",
                label="Date of loss",
                section="loss",
                critical=True,
                state="conflicting",
                detail="The loss is dated after the notification was received.",
            )
        )

    estimate = getattr(case, "estimated_loss_minor", None)
    repair = getattr(case, "repair_estimate_minor", None)
    if estimate and repair and repair > estimate * 3:
        conflicts.append(
            FieldStatus(
                path="financial.repair_estimate",
                label="Repair estimate",
                section="financial",
                critical=False,
                state="conflicting",
                detail="The repair estimate is more than three times the reported loss.",
            )
        )

    fatalities = getattr(case, "fatalities", None) or 0
    injuries = getattr(case, "injuries", None)
    if fatalities and injuries == 0:
        conflicts.append(
            FieldStatus(
                path="loss.injuries",
                label="Injuries reported",
                section="loss",
                critical=False,
                state="conflicting",
                detail="Fatalities are reported alongside zero injuries.",
            )
        )

    return conflicts


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Factor:
    """One thing that moved an assessment, and by how much."""

    key: str
    detail: str
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {"factor": self.key, "detail": self.detail, "weight": round(self.weight, 3)}


@dataclass(slots=True)
class SeverityResult:
    severity: Severity
    confidence: float
    factors: list[Factor] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "confidence": round(self.confidence, 4),
            "factors": [factor.as_dict() for factor in self.factors],
        }


def assess_severity(
    case: Any,
    *,
    config: FNOLSettings,
    policy_limit_minor: int | None = None,
    cat_matched: bool = False,
) -> SeverityResult:
    """The band this loss most likely sits in, and why.

    Money sets the floor; the human and structural facts can only push it up.
    That asymmetry is intentional: a fatality on a £5,000 claim is not a low
    severity claim, but a large estimate on an otherwise unremarkable loss is
    still a large claim.
    """
    factors: list[Factor] = []
    estimate = getattr(case, "estimated_loss_minor", None)

    if estimate is None:
        band = Severity.MEDIUM
        confidence = 0.35
        factors.append(
            Factor("no_estimate", "No estimated loss was stated; banded provisionally.", 0.0)
        )
    else:
        if estimate >= config.severity_critical_threshold_minor:
            band = Severity.CRITICAL
        elif estimate >= config.severity_high_threshold_minor:
            band = Severity.HIGH
        elif estimate >= config.severity_medium_threshold_minor:
            band = Severity.MEDIUM
        else:
            band = Severity.LOW
        confidence = 0.7
        factors.append(
            Factor(
                "estimated_loss",
                f"Estimated loss of {estimate / 100:,.0f} {getattr(case, 'currency', 'GBP')}.",
                0.5,
            )
        )

    escalations: list[tuple[bool, Severity, str, str, float]] = [
        (
            bool(getattr(case, "fatalities", None)),
            Severity.CRITICAL,
            "fatalities",
            f"{getattr(case, 'fatalities', 0)} fatalities reported.",
            1.0,
        ),
        (
            (getattr(case, "injuries", None) or 0) >= 3,
            Severity.HIGH,
            "multiple_injuries",
            f"{getattr(case, 'injuries', 0)} people injured.",
            0.6,
        ),
        (
            bool(getattr(case, "injuries", None)),
            Severity.MEDIUM,
            "injuries",
            "Injuries reported.",
            0.35,
        ),
        (
            bool(getattr(case, "business_interruption", None)),
            Severity.HIGH,
            "business_interruption",
            "Business interruption reported.",
            0.5,
        ),
        (
            bool(getattr(case, "structural_damage", None)),
            Severity.HIGH,
            "structural_damage",
            "Structural damage reported.",
            0.45,
        ),
        (
            bool(getattr(case, "environmental_exposure", None)),
            Severity.HIGH,
            "environmental_exposure",
            "Environmental exposure reported.",
            0.5,
        ),
        (
            bool(getattr(case, "potential_litigation", None)),
            Severity.HIGH,
            "litigation",
            "Legal involvement indicated.",
            0.4,
        ),
        (cat_matched, Severity.HIGH, "catastrophe", "Attributed to a catastrophe event.", 0.4),
    ]

    for applies, floor, key, detail, weight in escalations:
        if not applies:
            continue
        factors.append(Factor(key, detail, weight))
        if _rank(floor) > _rank(band):
            band = floor
        confidence = min(0.95, confidence + 0.05)

    if policy_limit_minor and estimate and estimate >= policy_limit_minor * 0.75:
        factors.append(
            Factor(
                "near_policy_limit",
                "The estimate is at or near the policy limit.",
                0.5,
            )
        )
        if _rank(Severity.HIGH) > _rank(band):
            band = Severity.HIGH

    return SeverityResult(severity=band, confidence=round(confidence, 2), factors=factors)


def _rank(severity: Severity) -> int:
    return {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}[severity]


# ---------------------------------------------------------------------------
# Fraud indicators
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FraudIndicator:
    code: str
    title: str
    detail: str
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "weight": round(self.weight, 3),
        }


@dataclass(slots=True)
class FraudResult:
    level: RiskLevel
    score: float
    indicators: list[FraudIndicator] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "score": round(self.score, 4),
            "indicators": [indicator.as_dict() for indicator in self.indicators],
        }


def assess_fraud_indicators(
    case: Any,
    *,
    config: FNOLSettings,
    policy_effective: date | None,
    policy_expiry: date | None,
    duplicate_count: int,
    conflicting_fields: int,
    human_corrections: int,
    unreadable_documents: int,
) -> FraudResult:
    """Signals worth a second look. Not a determination, and named accordingly.

    Each indicator is a fact about the notification, stated plainly enough that an
    officer can dismiss it. A policy incepted last week is not evidence of fraud —
    it is a reason to read the file properly, and that is the strongest claim this
    function makes.
    """
    indicators: list[FraudIndicator] = []
    loss_date = getattr(case, "date_of_loss", None)
    loss_day = loss_date.date() if isinstance(loss_date, datetime) else loss_date

    if policy_effective and loss_day:
        days_since_inception = (loss_day - policy_effective).days
        if 0 <= days_since_inception <= config.policy_edge_window_days:
            indicators.append(
                FraudIndicator(
                    "loss_near_inception",
                    "Loss shortly after inception",
                    f"The loss occurred {days_since_inception} days after the policy incepted.",
                    0.3,
                )
            )
    if policy_expiry and loss_day:
        days_to_expiry = (policy_expiry - loss_day).days
        if 0 <= days_to_expiry <= config.policy_edge_window_days:
            indicators.append(
                FraudIndicator(
                    "loss_near_expiry",
                    "Loss shortly before expiry",
                    f"The loss occurred {days_to_expiry} days before the policy expires.",
                    0.2,
                )
            )

    received = getattr(case, "received_at", None)
    if loss_date and received:
        delay_days = (received - loss_date).days
        if delay_days >= 30:
            indicators.append(
                FraudIndicator(
                    "late_notification",
                    "Late notification",
                    f"The loss was notified {delay_days} days after it occurred.",
                    0.25,
                )
            )

    if duplicate_count:
        indicators.append(
            FraudIndicator(
                "duplicate_submission",
                "Repeated submission",
                f"{duplicate_count} closely matching record(s) already exist.",
                0.3,
            )
        )

    if conflicting_fields:
        indicators.append(
            FraudIndicator(
                "inconsistent_information",
                "Inconsistent information",
                f"{conflicting_fields} field(s) on the notice contradict each other.",
                0.25,
            )
        )

    if human_corrections >= 5:
        indicators.append(
            FraudIndicator(
                "supplied_information_changed",
                "Supplied information changed repeatedly",
                f"{human_corrections} extracted values have been overwritten since intake.",
                0.15,
            )
        )

    if unreadable_documents:
        indicators.append(
            FraudIndicator(
                "document_inconsistency",
                "Documents could not be read",
                f"{unreadable_documents} attachment(s) yielded no readable content.",
                0.1,
            )
        )

    # Three content words or fewer. Deliberately strict: a short account of a
    # small loss is normal, and an indicator that fires on every windscreen claim
    # is an indicator officers learn to ignore.
    description = normalise(getattr(case, "loss_description", None) or "")
    if description and len(tokens(description)) < 4:
        indicators.append(
            FraudIndicator(
                "sparse_description",
                "Unusually sparse description",
                "The circumstances of the loss are described in very few words.",
                0.1,
            )
        )

    # Combined as diminishing returns rather than a sum: five weak signals should
    # not out-score one strong one, and no set of indicators reaches certainty.
    score = 0.0
    for indicator in sorted(indicators, key=lambda item: item.weight, reverse=True):
        score += indicator.weight * (1.0 - score)

    if score >= config.fraud_high_threshold:
        level = RiskLevel.HIGH
    elif score >= config.fraud_medium_threshold:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW

    return FraudResult(level=level, score=round(score, 4), indicators=indicators)


# ---------------------------------------------------------------------------
# Coverage indicators
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CoverageCheck:
    key: str
    label: str
    #: `pass`, `attention`, `fail` or `unknown`.
    state: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "state": self.state, "detail": self.detail}


@dataclass(slots=True)
class CoverageResult:
    indicator: CoverageIndicator
    confidence: float
    checks: list[CoverageCheck] = field(default_factory=list)
    reasoning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "indicator": self.indicator.value,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "checks": [check.as_dict() for check in self.checks],
        }


def assess_coverage(case: Any, policy: Any | None, *, policy_confirmed: bool) -> CoverageResult:
    """A preliminary read of the policy against the loss.

    Never a coverage decision, and the wording throughout says so. The strongest
    outcome available is "likely covered", and it requires a *bound* policy that
    was in force, a peril the policy names, and an estimate under the limit.
    "Bound" means either an exact policy-number match or an officer's
    confirmation — a policy the matcher merely ranked highly is not enough, and
    the verdict says "review required" rather than pretending otherwise.
    """
    if policy is None:
        return CoverageResult(
            indicator=CoverageIndicator.POLICY_NOT_LOCATED,
            confidence=0.9,
            reasoning="No policy has been matched to this notification yet.",
            checks=[
                CoverageCheck(
                    "policy", "Policy located", "fail", "No policy record has been matched."
                )
            ],
        )

    checks: list[CoverageCheck] = []
    loss_date = getattr(case, "date_of_loss", None)
    loss_day = loss_date.date() if isinstance(loss_date, datetime) else loss_date

    in_force: bool | None = None
    if loss_day is None:
        checks.append(
            CoverageCheck(
                "in_force",
                "Policy in force on the date of loss",
                "unknown",
                "The date of loss has not been established.",
            )
        )
    else:
        in_force = policy.effective_date <= loss_day <= policy.expiry_date
        checks.append(
            CoverageCheck(
                "in_force",
                "Policy in force on the date of loss",
                "pass" if in_force else "fail",
                (
                    f"Cover ran {policy.effective_date:%d %b %Y} to {policy.expiry_date:%d %b %Y};"
                    f" the loss is dated {loss_day:%d %b %Y}."
                ),
            )
        )

    status_ok = (policy.status or "active").lower() == "active"
    checks.append(
        CoverageCheck(
            "policy_status",
            "Policy status",
            "pass" if status_ok else "attention",
            f"The policy is recorded as {policy.status}.",
        )
    )

    peril_state, peril_detail = _peril_check(case, policy)
    checks.append(CoverageCheck("peril", "Reported cause against cover", peril_state, peril_detail))

    limit_state, limit_detail = _limit_check(case, policy)
    checks.append(CoverageCheck("limit", "Estimate against the limit", limit_state, limit_detail))

    if policy.deductible_amount_minor:
        checks.append(
            CoverageCheck(
                "deductible",
                "Deductible",
                "pass",
                f"{policy.deductible_amount_minor / 100:,.0f} {policy.currency} applies.",
            )
        )

    location_state, location_detail = _location_check(case, policy)
    checks.append(
        CoverageCheck(
            "location", "Loss location against the policy", location_state, location_detail
        )
    )

    return _coverage_verdict(checks, policy_confirmed=policy_confirmed, in_force=in_force)


def _mentions(haystack_tokens: set[str], phrase: str) -> bool:
    """Whether a peril phrase is present as words rather than as characters.

    Substring matching would find the exclusion "war" inside "ransomware" and
    decline a covered cyber claim — which is exactly the kind of quiet, confident
    wrongness this module must not produce.
    """
    phrase_tokens = tokens(phrase, drop_stopwords=False)
    return bool(phrase_tokens) and phrase_tokens <= haystack_tokens


def _peril_check(case: Any, policy: Any) -> tuple[str, str]:
    perils = [normalise(peril) for peril in (policy.perils_covered or [])]
    exclusions = [normalise(exclusion) for exclusion in (policy.exclusions or [])]
    haystack = normalise(
        " ".join(
            part
            for part in (
                getattr(case, "cause_of_loss", None),
                getattr(case, "loss_type", None),
                getattr(case, "loss_description", None),
            )
            if part
        )
    )

    if not haystack:
        return "unknown", "The cause of loss has not been established."

    haystack_tokens = tokens(haystack, drop_stopwords=False)

    hit_exclusion = next(
        (exclusion for exclusion in exclusions if _mentions(haystack_tokens, exclusion)), None
    )
    if hit_exclusion:
        return "fail", f"The notification mentions an excluded peril: {hit_exclusion}."

    hit_peril = next((peril for peril in perils if _mentions(haystack_tokens, peril)), None)
    if hit_peril:
        return "pass", f"The reported cause matches the covered peril “{hit_peril}”."

    if not perils:
        return "unknown", "The matched policy does not list its covered perils."
    return "attention", "The reported cause does not obviously match a listed peril."


def _limit_check(case: Any, policy: Any) -> tuple[str, str]:
    estimate = getattr(case, "estimated_loss_minor", None)
    limit = policy.limit_amount_minor
    if estimate is None:
        return "unknown", "No estimated loss has been stated."
    if not limit:
        return "unknown", "The matched policy does not record a limit."
    if estimate > limit:
        return (
            "fail",
            f"The estimate of {estimate / 100:,.0f} exceeds the limit of {limit / 100:,.0f}.",
        )
    if estimate > limit * 0.75:
        return "attention", "The estimate is within 25% of the policy limit."
    return "pass", f"The estimate is within the {limit / 100:,.0f} {policy.currency} limit."


def _location_check(case: Any, policy: Any) -> tuple[str, str]:
    loss_location = getattr(case, "loss_location", None)
    if not loss_location:
        return "unknown", "No loss location has been established."

    candidates = [policy.primary_location, policy.region, policy.country]
    candidates += [
        entry.get("address") if isinstance(entry, dict) else None
        for entry in (policy.locations or [])
    ]
    loss_tokens = tokens(loss_location)
    for candidate in candidates:
        if candidate and tokens(candidate) & loss_tokens:
            return "pass", f"The loss location matches the insured location “{candidate}”."
    return "attention", "The loss location does not match any location on the policy."


def _coverage_verdict(
    checks: list[CoverageCheck], *, policy_confirmed: bool, in_force: bool | None
) -> CoverageResult:
    states = {check.key: check.state for check in checks}

    if states.get("in_force") == "fail":
        return CoverageResult(
            indicator=CoverageIndicator.POSSIBLE_EXCLUSION,
            confidence=0.85,
            checks=checks,
            reasoning="The date of loss falls outside the policy period.",
        )
    if states.get("peril") == "fail":
        return CoverageResult(
            indicator=CoverageIndicator.POSSIBLE_EXCLUSION,
            confidence=0.7,
            checks=checks,
            reasoning="The notification mentions a peril the policy excludes.",
        )
    if in_force is None or states.get("peril") == "unknown":
        return CoverageResult(
            indicator=CoverageIndicator.INSUFFICIENT_INFORMATION,
            confidence=0.6,
            checks=checks,
            reasoning="Not enough of the loss has been established to indicate cover.",
        )

    attention = [check for check in checks if check.state == "attention"]
    if attention or not policy_confirmed:
        reason = (
            "The policy match has not been confirmed by an officer."
            if not policy_confirmed
            else "; ".join(check.detail for check in attention)
        )
        return CoverageResult(
            indicator=CoverageIndicator.REVIEW_REQUIRED,
            confidence=0.6,
            checks=checks,
            reasoning=reason,
        )

    return CoverageResult(
        indicator=CoverageIndicator.LIKELY_COVERED,
        confidence=0.75,
        checks=checks,
        reasoning="The policy was in force, the cause matches a covered peril, "
        "and the estimate is within the limit.",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _line_of_business(case: Any) -> LineOfBusiness | None:
    raw = getattr(case, "line_of_business", None)
    if not raw:
        return None
    try:
        return LineOfBusiness(raw)
    except ValueError:
        return None


def text_signals(case: Any, extra_text: str = "") -> str:
    """Everything on the case a keyword rule should read, as one lowercase string."""
    parts = [
        getattr(case, "loss_description", None),
        getattr(case, "cause_of_loss", None),
        getattr(case, "affected_assets", None),
        getattr(case, "authorities_involved", None),
        getattr(case, "source_body", None),
        extra_text,
    ]
    return normalise(" ".join(part for part in parts if part))


def has_litigation_signal(text: str) -> bool:
    return any(signal in text for signal in LITIGATION_SIGNALS)


def has_serious_injury_signal(text: str) -> bool:
    return any(signal in text for signal in INJURY_SIGNALS)


def utc_today() -> date:
    return datetime.now(UTC).date()
