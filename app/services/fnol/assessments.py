"""Completeness, severity, fraud and coverage, applied to a case.

Thin by design: every rule lives in `app.domain.assessment`, and this is what
gathers the inputs those rules need and writes their conclusions back. Grouped
into one service because they run together, read the same inputs, and are
meaningless individually — a severity band computed from an incomplete notice has
to be read next to the completeness figure that qualifies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import FNOLSettings, settings
from app.core.logging import get_logger
from app.domain import assessment as rules
from app.domain.enums import DocumentExtractionStatus
from app.repositories.fnol import FNOLRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class AssessmentOutcome:
    completeness: rules.CompletenessResult
    severity: rules.SeverityResult
    fraud: rules.FraudResult
    coverage: rules.CoverageResult


class AssessmentServices:
    """Runs the four deterministic assessments over a case."""

    def __init__(self, repository: FNOLRepository, *, config: FNOLSettings | None = None) -> None:
        self._repository = repository
        self._config = config or settings.fnol

    async def run(
        self,
        case: Any,
        *,
        policy: Any | None,
        field_confidences: dict[str, float | None] | None = None,
        duplicate_count: int = 0,
        cat_matched: bool = False,
    ) -> AssessmentOutcome:
        documents = await self._repository.list_documents(case.id)
        parties = await self._repository.list_parties(case.id)
        fields = await self._repository.list_fields(case.id)

        confidences = field_confidences or {field.field_path: field.confidence for field in fields}
        human_corrections = sum(1 for field in fields if field.human_modified)
        unreadable = sum(
            1
            for document in documents
            if document.extraction_status
            in (DocumentExtractionStatus.FAILED, DocumentExtractionStatus.UNSUPPORTED)
        )

        completeness = rules.assess_completeness(
            case,
            document_count=len(documents),
            party_roles={party.role for party in parties},
            field_confidences=confidences,
            config=self._config,
        )

        severity = rules.assess_severity(
            case,
            config=self._config,
            policy_limit_minor=policy.limit_amount_minor if policy else None,
            # The limit is in the policy's currency and the estimate in the
            # notice's; the two are not the same claim often enough to matter.
            policy_currency=policy.currency if policy else None,
            cat_matched=cat_matched,
        )

        fraud = rules.assess_fraud_indicators(
            case,
            config=self._config,
            policy_effective=policy.effective_date if policy else None,
            policy_expiry=policy.expiry_date if policy else None,
            duplicate_count=duplicate_count,
            conflicting_fields=len(completeness.conflicting),
            human_corrections=human_corrections,
            unreadable_documents=unreadable,
        )

        # `policy_id` is set only by an exact match or by an officer confirming
        # one, so it — rather than the confirmation flag alone — is what "the
        # policy is settled" means to the coverage read.
        coverage = rules.assess_coverage(
            case, policy, policy_confirmed=bool(case.policy_id), config=self._config
        )

        case.completeness_score = completeness.score
        # An officer's severity override survives every re-run. The computed band
        # is still recorded in the analysis row, so the override remains visible
        # as an override rather than becoming the truth.
        if not case.severity_overridden:
            case.severity = severity.severity.value
            case.severity_confidence = severity.confidence
        case.fraud_risk = fraud.level.value
        case.fraud_score = fraud.score
        case.coverage_indicator = coverage.indicator.value

        logger.info(
            "fnol_assessed",
            reference=case.reference,
            completeness=round(completeness.score, 3),
            severity=case.severity,
            fraud=fraud.level.value,
            coverage=coverage.indicator.value,
        )

        return AssessmentOutcome(
            completeness=completeness, severity=severity, fraud=fraud, coverage=coverage
        )
