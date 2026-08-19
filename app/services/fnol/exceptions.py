"""The attention-required layer.

This is the module the FNOL officer actually works from. Everything the pipeline
learned is reduced here to a list of things that need a person, each with the one
sentence that says what and the one that says why — so an officer clears a notice
by reading eight lines rather than by checking forty fields.

Raising and clearing are both done here, together, on every run. That symmetry
matters: an exception whose cause has gone away has to disappear, or the list
stops being trustworthy and officers start ignoring it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import FNOLSettings, settings
from app.domain.enums import (
    CoverageIndicator,
    ExceptionCode,
    ExceptionSeverity,
    PolicyMatchStrength,
    RiskLevel,
    Severity,
)
from app.domain.lifecycle import BLOCKING_EXCEPTIONS
from app.domain.normalisation import is_future_date
from app.repositories.fnol import FNOLRepository


@dataclass(slots=True)
class RaisedException:
    code: ExceptionCode
    severity: ExceptionSeverity
    title: str
    detail: str
    context: dict[str, Any]

    @property
    def blocking(self) -> bool:
        return self.code in BLOCKING_EXCEPTIONS


class ExceptionService:
    def __init__(self, repository: FNOLRepository, *, config: FNOLSettings | None = None) -> None:
        self._repository = repository
        self._config = config or settings.fnol

    async def evaluate(
        self,
        case: Any,
        *,
        policy_strength: PolicyMatchStrength,
        policy_candidates: int,
        policy: Any | None,
        duplicates: list[Any],
        completeness: Any,
        severity: Any,
        fraud: Any,
        coverage: Any,
        cat_match: Any | None,
        extraction_failed: bool,
        extraction_confidence: float | None,
        unreadable_documents: int,
        raw_loss_date: str | None = None,
        #: The top candidate's reference agrees and its insured name does not.
        #: Defaulted so every existing caller and test is unaffected.
        identity_conflict: bool = False,
    ) -> list[RaisedException]:
        """Recompute the whole exception list and persist the difference."""
        raised = self._collect(
            case,
            policy_strength=policy_strength,
            policy_candidates=policy_candidates,
            identity_conflict=identity_conflict,
            policy=policy,
            duplicates=duplicates,
            completeness=completeness,
            severity=severity,
            fraud=fraud,
            coverage=coverage,
            cat_match=cat_match,
            extraction_failed=extraction_failed,
            extraction_confidence=extraction_confidence,
            unreadable_documents=unreadable_documents,
            raw_loss_date=raw_loss_date,
        )

        raised_codes = {exception.code for exception in raised}
        for exception in raised:
            await self._repository.upsert_exception(
                case.id,
                code=exception.code.value,
                severity=exception.severity.value,
                title=exception.title,
                detail=exception.detail,
                blocking=exception.blocking,
                context=exception.context,
            )

        for code in ExceptionCode:
            if code not in raised_codes:
                await self._repository.clear_exception(case.id, code.value)

        return raised

    def _collect(
        self,
        case: Any,
        *,
        policy_strength: PolicyMatchStrength,
        policy_candidates: int,
        policy: Any | None,
        duplicates: list[Any],
        completeness: Any,
        severity: Any,
        fraud: Any,
        coverage: Any,
        cat_match: Any | None,
        extraction_failed: bool,
        extraction_confidence: float | None,
        unreadable_documents: int,
        raw_loss_date: str | None,
        identity_conflict: bool = False,
    ) -> list[RaisedException]:
        raised: list[RaisedException] = []

        if extraction_failed:
            raised.append(
                RaisedException(
                    ExceptionCode.AI_PROCESSING_FAILED,
                    ExceptionSeverity.CRITICAL,
                    "Automated reading failed",
                    "The notification could not be read automatically. Enter the claim "
                    "details manually, or retry processing.",
                    {},
                )
            )

        # --- Policy ---------------------------------------------------------
        if not case.policy_confirmed:
            if identity_conflict:
                # Its own code, and it outranks the others: a reference that agrees
                # while the insured named does not is not "choose between these
                # candidates", it is "do not bind anything until you have read the
                # schedule". A policy number is the strongest signal the engine has,
                # so a notice that carries one and still names the wrong client is
                # the one case where the strongest signal is the least trustworthy.
                raised.append(
                    RaisedException(
                        ExceptionCode.POLICY_IDENTITY_CONFLICT,
                        ExceptionSeverity.CRITICAL,
                        "Policy reference and insured do not agree",
                        "The reference quoted matches a policy, but the insured named on "
                        "the notification is not the insured on that policy. Check the "
                        "notice against the schedule before binding it.",
                        {"candidates": policy_candidates},
                    )
                )
            elif policy_strength is PolicyMatchStrength.NONE:
                raised.append(
                    RaisedException(
                        ExceptionCode.NO_POLICY_MATCH,
                        ExceptionSeverity.CRITICAL,
                        "No policy matched",
                        "No policy in the book matches the details on this notification. "
                        "Search for the policy and select it, or refer the notice.",
                        {"candidates": policy_candidates},
                    )
                )
            elif policy_strength is PolicyMatchStrength.POSSIBLE and policy_candidates > 1:
                raised.append(
                    RaisedException(
                        ExceptionCode.MULTIPLE_POLICY_MATCHES,
                        ExceptionSeverity.WARNING,
                        f"{policy_candidates} possible policies",
                        "More than one policy fits this notification. Choose the correct "
                        "one before the claim is created.",
                        {"candidates": policy_candidates},
                    )
                )
            elif policy_strength in (PolicyMatchStrength.HIGH, PolicyMatchStrength.POSSIBLE):
                raised.append(
                    RaisedException(
                        ExceptionCode.UNCONFIRMED_POLICY_MATCH,
                        ExceptionSeverity.WARNING,
                        "Policy match needs confirming",
                        "A policy was matched on partial details. Confirm it is the right "
                        "one before the claim is created.",
                        {"strength": policy_strength.value},
                    )
                )

        # --- Dates ----------------------------------------------------------
        if is_future_date(raw_loss_date) or (
            case.date_of_loss and case.received_at and case.date_of_loss > case.received_at
        ):
            raised.append(
                RaisedException(
                    ExceptionCode.FUTURE_LOSS_DATE,
                    ExceptionSeverity.CRITICAL,
                    "Date of loss is in the future",
                    "The date of loss reads as later than the date the notification "
                    "arrived. Correct the date before creating the claim.",
                    {"reported": raw_loss_date},
                )
            )

        if completeness.conflicting:
            raised.append(
                RaisedException(
                    ExceptionCode.CONFLICTING_DATES,
                    ExceptionSeverity.WARNING,
                    "Conflicting information",
                    "; ".join(
                        status.detail for status in completeness.conflicting if status.detail
                    ),
                    {"fields": [status.path for status in completeness.conflicting]},
                )
            )

        if policy is not None and case.date_of_loss is not None:
            loss_day = case.date_of_loss.date()
            if not (policy.effective_date <= loss_day <= policy.expiry_date):
                raised.append(
                    RaisedException(
                        ExceptionCode.OUTSIDE_POLICY_PERIOD,
                        ExceptionSeverity.CRITICAL,
                        "Loss falls outside the policy period",
                        f"The policy ran {policy.effective_date:%d %b %Y} to "
                        f"{policy.expiry_date:%d %b %Y}; the loss is dated "
                        f"{loss_day:%d %b %Y}.",
                        {"policy_number": policy.policy_number},
                    )
                )

        if (
            policy is not None
            and policy.limit_amount_minor
            and case.estimated_loss_minor
            and case.estimated_loss_minor > policy.limit_amount_minor
        ):
            raised.append(
                RaisedException(
                    ExceptionCode.EXCEEDS_POLICY_LIMIT,
                    ExceptionSeverity.WARNING,
                    "Estimate exceeds the policy limit",
                    f"The estimated loss of {case.estimated_loss_minor / 100:,.0f} "
                    f"{case.currency} is above the "
                    f"{policy.limit_amount_minor / 100:,.0f} {policy.currency} limit.",
                    {"limit_minor": policy.limit_amount_minor},
                )
            )

        # --- Completeness ---------------------------------------------------
        missing_critical = completeness.missing_critical
        if missing_critical:
            labels = ", ".join(status.label for status in missing_critical)
            raised.append(
                RaisedException(
                    ExceptionCode.MISSING_CRITICAL_INFORMATION,
                    ExceptionSeverity.CRITICAL,
                    f"{len(missing_critical)} required field(s) missing",
                    f"The claim cannot be created without: {labels}.",
                    {"fields": [status.path for status in missing_critical]},
                )
            )

        # --- Duplicates -----------------------------------------------------
        unresolved = [
            candidate
            for candidate in duplicates
            if getattr(candidate, "resolution", "unresolved") == "unresolved"
        ]
        if unresolved:
            best = max(unresolved, key=lambda candidate: float(candidate.score))
            raised.append(
                RaisedException(
                    ExceptionCode.POSSIBLE_DUPLICATE,
                    ExceptionSeverity.CRITICAL
                    if float(best.score) >= self._config.duplicate_strong_threshold
                    else ExceptionSeverity.WARNING,
                    f"Possible duplicate of {best.candidate_reference}",
                    f"This notification matches {best.candidate_reference} at "
                    f"{float(best.score):.0%}. Decide whether to continue as a new claim, "
                    "link it, or mark it a duplicate.",
                    {
                        "reference": best.candidate_reference,
                        "score": float(best.score),
                        "count": len(unresolved),
                    },
                )
            )

        # --- Risk signals ---------------------------------------------------
        if severity.severity in (Severity.HIGH, Severity.CRITICAL):
            raised.append(
                RaisedException(
                    ExceptionCode.HIGH_SEVERITY,
                    ExceptionSeverity.WARNING,
                    f"{severity.severity.value.title()} severity assessed",
                    "; ".join(factor.detail for factor in severity.factors[:3]),
                    {"severity": severity.severity.value},
                )
            )

        if fraud.level in (RiskLevel.MEDIUM, RiskLevel.HIGH):
            raised.append(
                RaisedException(
                    ExceptionCode.FRAUD_INDICATOR,
                    ExceptionSeverity.WARNING,
                    f"{fraud.level.value.title()} fraud indicators",
                    "; ".join(indicator.title for indicator in fraud.indicators[:3])
                    + ". These are signals to review, not a finding of fraud.",
                    {"level": fraud.level.value, "score": fraud.score},
                )
            )

        if coverage.indicator in (
            CoverageIndicator.POSSIBLE_EXCLUSION,
            CoverageIndicator.REVIEW_REQUIRED,
        ):
            raised.append(
                RaisedException(
                    ExceptionCode.COVERAGE_ISSUE,
                    ExceptionSeverity.WARNING
                    if coverage.indicator is CoverageIndicator.REVIEW_REQUIRED
                    else ExceptionSeverity.CRITICAL,
                    "Coverage needs review"
                    if coverage.indicator is CoverageIndicator.REVIEW_REQUIRED
                    else "Possible exclusion",
                    coverage.reasoning,
                    {"indicator": coverage.indicator.value},
                )
            )

        if cat_match is not None and not case.cat_confirmed:
            raised.append(
                RaisedException(
                    ExceptionCode.CAT_MATCH,
                    ExceptionSeverity.INFO,
                    f"Possible catastrophe match — {cat_match.reference}",
                    f"{cat_match.name} at {cat_match.confidence:.0%} confidence. "
                    "Confirm or remove the attribution.",
                    {"reference": cat_match.reference, "confidence": cat_match.confidence},
                )
            )

        if (
            extraction_confidence is not None
            and extraction_confidence < self._config.low_confidence_threshold
            and not extraction_failed
        ):
            raised.append(
                RaisedException(
                    ExceptionCode.LOW_EXTRACTION_CONFIDENCE,
                    ExceptionSeverity.WARNING,
                    "Low confidence in the automated reading",
                    f"The notification was read with {extraction_confidence:.0%} overall "
                    "confidence. Check the extracted fields against the source.",
                    {"confidence": extraction_confidence},
                )
            )

        if unreadable_documents:
            raised.append(
                RaisedException(
                    ExceptionCode.DOCUMENT_UNREADABLE,
                    ExceptionSeverity.INFO,
                    f"{unreadable_documents} document(s) could not be read",
                    "Their contents were not included in the automated reading. Check "
                    "them manually, or ask for a text-based copy.",
                    {"count": unreadable_documents},
                )
            )

        return raised
