"""Working out what kind of claim this is.

Two readers, and the deterministic one is not a fallback: the keyword classifier
always runs, and when a model is configured its answer is checked against it. A
model naming a line the carrier does not write is rejected outright; a model
disagreeing with a strong keyword signal lowers the confidence rather than the
answer, because the words in the notice are evidence and the model has seen more
of them than the keyword table has.

Nothing here overwrites a classification an officer has set. That is checked
before the call is made, so an override also saves the cost of one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.domain.enums import LineOfBusiness
from app.domain.extraction import ClassificationResult
from app.domain.heuristics import classify_from_text
from app.domain.normalisation import parse_line_of_business
from app.domain.rules import valid_loss_type
from app.services.ai.base import AIProvider, AIProviderError

logger = get_logger(__name__)

HEURISTIC_PROVIDER = "heuristic"

SYSTEM_PROMPT = (
    "You classify insurance claim notifications. Answer only with values from the "
    "vocabulary you are given. If the notification does not clearly belong to one of "
    "them, answer 'unknown' — inventing a line of business is worse than admitting "
    "uncertainty. Explain your answer in one sentence, citing the words you relied on."
)

COMPLEXITIES = frozenset({"simple", "standard", "complex"})


@dataclass(slots=True)
class ClassificationOutcome:
    line_of_business: LineOfBusiness
    claim_type: str | None
    loss_type: str | None
    complexity: str
    confidence: float
    reasoning: str
    provider: str
    model: str | None
    #: Set when the model's answer was rejected or downgraded, so the officer can
    #: see that the two readers disagreed rather than only seeing the result.
    caveat: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_of_business": self.line_of_business.value,
            "claim_type": self.claim_type,
            "loss_type": self.loss_type,
            "complexity": self.complexity,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "provider": self.provider,
            "caveat": self.caveat,
        }


class ClassificationService:
    def __init__(self, *, provider: AIProvider | None) -> None:
        self._provider = provider

    async def classify(
        self, *, text: str, policy_line: LineOfBusiness | None = None
    ) -> ClassificationOutcome:
        baseline = classify_from_text(text, policy_line=policy_line)

        if self._provider is None:
            return self._from_result(baseline, provider=HEURISTIC_PROVIDER, model=None)

        try:
            response = await self._provider.structured(
                schema=ClassificationResult,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=self._prompt(text, policy_line),
                schema_name="claim_classification",
            )
        except AIProviderError as exc:
            logger.warning("fnol_classification_provider_failed", error=str(exc))
            outcome = self._from_result(baseline, provider=HEURISTIC_PROVIDER, model=None)
            outcome.caveat = "Classified deterministically: the model provider was unavailable."
            return outcome

        return self._reconcile(
            response.data, baseline, provider=response.provider, model=response.model
        )

    def _reconcile(
        self,
        model_result: ClassificationResult,
        baseline: ClassificationResult,
        *,
        provider: str,
        model: str | None,
    ) -> ClassificationOutcome:
        line = parse_line_of_business(model_result.line_of_business)
        if line is None:
            outcome = self._from_result(baseline, provider=HEURISTIC_PROVIDER, model=None)
            outcome.caveat = (
                f"The model proposed “{model_result.line_of_business}”, which is not a line "
                "this carrier writes. The deterministic classification was used instead."
            )
            return outcome

        baseline_line = parse_line_of_business(baseline.line_of_business)
        caveat: str | None = None
        confidence = model_result.confidence

        if (
            baseline_line
            and baseline_line is not LineOfBusiness.UNKNOWN
            and baseline_line is not line
            and baseline.confidence >= 0.6
        ):
            confidence = min(confidence, 0.55)
            caveat = (
                f"The wording of the notification also matches "
                f"{baseline_line.value.replace('_', ' ')}; confirm the line before creating "
                "the claim."
            )

        return ClassificationOutcome(
            line_of_business=line,
            claim_type=(model_result.claim_type or "").strip()[:64] or None,
            loss_type=valid_loss_type(line, model_result.loss_type)
            or valid_loss_type(line, baseline.loss_type),
            complexity=model_result.complexity
            if model_result.complexity in COMPLEXITIES
            else "standard",
            confidence=confidence,
            reasoning=model_result.reasoning.strip()[:1000],
            provider=provider,
            model=model,
            caveat=caveat,
        )

    def _from_result(
        self, result: ClassificationResult, *, provider: str, model: str | None
    ) -> ClassificationOutcome:
        line = parse_line_of_business(result.line_of_business) or LineOfBusiness.UNKNOWN
        return ClassificationOutcome(
            line_of_business=line,
            claim_type=result.claim_type,
            loss_type=valid_loss_type(line, result.loss_type),
            complexity=result.complexity if result.complexity in COMPLEXITIES else "standard",
            confidence=result.confidence,
            reasoning=result.reasoning,
            provider=provider,
            model=model,
        )

    def _prompt(self, text: str, policy_line: LineOfBusiness | None) -> str:
        lines = ", ".join(
            line.value for line in LineOfBusiness if line is not LineOfBusiness.UNKNOWN
        )
        return (
            f"Lines of business: {lines}, unknown.\n"
            "Complexity must be one of: simple, standard, complex.\n"
            + (
                f"The matched policy is written on the {policy_line.value} line.\n"
                if policy_line
                else ""
            )
            + "\nClassify this notification:\n\n"
            + text[:12_000]
        )
