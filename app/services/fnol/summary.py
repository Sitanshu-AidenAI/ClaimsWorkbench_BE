"""The executive paragraph at the head of the review screen.

Regenerated only when the facts behind it change — the summary's fingerprint is
built from the values it actually mentions, so an officer adding a note or
resolving an exception does not cost a model call. Everything the summary states
comes from the case, never from the model's memory of it: the prompt is a list of
established facts, and the model is asked to write them as prose, not to decide
what they are.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.domain.extraction import SummaryResult
from app.services.ai.base import AIProvider, AIProviderError

logger = get_logger(__name__)

HEURISTIC_PROVIDER = "heuristic"

SYSTEM_PROMPT = (
    "You write the opening paragraph of a claims file for an insurance claims "
    "officer. Use only the facts you are given. Do not add caveats, "
    "recommendations, or anything not in the facts. Write 2-4 sentences of plain "
    "British English in the third person, no bullet points, no salutation. "
    "State amounts and dates exactly as given."
)


@dataclass(slots=True)
class SummaryOutcome:
    summary: str
    key_points: list[str]
    provider: str
    model: str | None
    fingerprint: str


class FNOLSummaryService:
    def __init__(self, *, provider: AIProvider | None) -> None:
        self._provider = provider

    @staticmethod
    def fingerprint(facts: dict[str, Any]) -> str:
        payload = "|".join(f"{key}={facts[key]}" for key in sorted(facts))
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    async def summarise(self, facts: dict[str, Any]) -> SummaryOutcome:
        fingerprint = self.fingerprint(facts)

        if self._provider is None:
            return self._deterministic(facts, fingerprint)

        try:
            response = await self._provider.structured(
                schema=SummaryResult,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=self._prompt(facts),
                schema_name="fnol_summary",
                max_output_tokens=600,
            )
        except AIProviderError as exc:
            logger.warning("fnol_summary_provider_failed", error=str(exc))
            return self._deterministic(facts, fingerprint)

        return SummaryOutcome(
            summary=response.data.summary.strip()[:2000],
            key_points=[point.strip()[:200] for point in response.data.key_points[:6]],
            provider=response.provider,
            model=response.model,
            fingerprint=fingerprint,
        )

    def _prompt(self, facts: dict[str, Any]) -> str:
        lines = "\n".join(
            f"- {key.replace('_', ' ')}: {value}" for key, value in facts.items() if value
        )
        return f"Facts established about this notification:\n{lines}\n\nWrite the paragraph."

    def _deterministic(self, facts: dict[str, Any], fingerprint: str) -> SummaryOutcome:
        """The same paragraph, assembled from the facts rather than written.

        Reads as a claims file entry because the sentence order is a claims file's
        order: what was reported and by whom, when and where it happened, what it
        is thought to cost, and what is still outstanding.
        """
        sentences: list[str] = []

        # The description stands as its own sentence rather than being spliced
        # into the reporting clause: a broker's prose does not survive being used
        # as the subject of somebody else's sentence.
        description = str(facts.get("loss_summary") or "").strip()
        if description:
            sentences.append(description if description.endswith(".") else f"{description}.")

        reporter = facts.get("reported_by")
        policy = facts.get("policy_number")
        insured = facts.get("insured")
        line = (
            ("Reported" if description else "A loss was reported")
            + (f" by {reporter}" if reporter else "")
            + (f" for {insured}" if insured else "")
            + (f" against policy {policy}" if policy else ", with no policy identified")
            + (f" at {facts['loss_location']}" if facts.get("loss_location") else "")
            + (f", loss dated {facts['date_of_loss']}" if facts.get("date_of_loss") else "")
        )
        sentences.append(f"{line}.")

        if facts.get("estimated_loss"):
            sentences.append(f"Initial estimated loss is {facts['estimated_loss']}.")

        if facts.get("policy_status"):
            sentences.append(str(facts["policy_status"]))

        if facts.get("cat_event"):
            sentences.append(f"The loss may be attributable to {facts['cat_event']}.")

        if facts.get("casualties"):
            sentences.append(str(facts["casualties"]))

        if facts.get("completeness"):
            sentences.append(str(facts["completeness"]))

        key_points = [
            str(value) for key, value in facts.items() if key.startswith("point_") and value
        ]

        return SummaryOutcome(
            summary=" ".join(sentences),
            key_points=key_points,
            provider=HEURISTIC_PROVIDER,
            model=None,
            fingerprint=fingerprint,
        )
