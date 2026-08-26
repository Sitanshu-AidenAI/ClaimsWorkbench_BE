"""Reading a notification into structured fields.

One extraction call for the whole notice, then deterministic normalisation of
every value it returned. The service never writes a value it could not parse, and
never overwrites a value a human has corrected — both of those rules live in the
repository's `upsert_field`, and this service is what feeds it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import AISettings, settings
from app.core.logging import get_logger
from app.domain import normalisation
from app.domain.assessment import FIELD_READERS
from app.domain.enums import FieldSource
from app.domain.extraction import ExtractedField, FNOLExtraction
from app.domain.heuristics import extract_from_text
from app.domain.rules import (
    BASE_REQUIRED_FIELDS,
    LOB_REQUIRED_FIELDS,
    SIGNAL_REQUIRED_FIELDS,
)
from app.repositories.fnol import FNOLRepository
from app.services.ai.base import AIProvider, AIProviderError
from app.services.fnol.evidence import SECTION_QUERIES, EvidenceBundle, FNOLEvidenceService

logger = get_logger(__name__)

HEURISTIC_PROVIDER = "heuristic"

SYSTEM_PROMPT = (
    "You are an insurance First Notice of Loss intake assistant. You read claim "
    "notifications — broker emails, portal submissions, call notes and attachments — "
    "and return only what the source actually states.\n"
    "Rules you must follow:\n"
    "1. Never infer a policy number, claim reference or party that is not written in the source.\n"
    "2. Leave a field null rather than guessing. A null is a correct answer.\n"
    "3. Quote the phrase you read each value from in `evidence`, verbatim and short.\n"
    "4. Set confidence to 1.0 only for values stated explicitly and unambiguously; "
    "use 0.5-0.8 when you inferred the value from context; 0 when absent.\n"
    "5. Copy values as written. Do not reformat dates, convert currencies or expand "
    "abbreviations.\n"
    "6. When a value comes from a labelled passage, set `source_chunk_ref` to that "
    "passage's label exactly as printed, for example 'C3'. Set it to null when the "
    "value came from the notification body rather than from a passage. Never invent "
    "a label: a citation that points nowhere is worse than no citation."
)

#: Label and section for each field path, so provenance rows read the same way the
#: review screen groups them.
_FIELD_META: dict[str, tuple[str, str]] = {
    requirement.path: (requirement.label, requirement.section)
    for requirement in (
        *BASE_REQUIRED_FIELDS,
        *(field for fields in LOB_REQUIRED_FIELDS.values() for field in fields),
        *(field for fields in SIGNAL_REQUIRED_FIELDS.values() for field in fields),
    )
}
_FIELD_META.update(
    {
        "notification.reporter_organisation": ("Reporter organisation", "notification"),
        "notification.reporter_role": ("Reporter role", "notification"),
        "notification.reporter_phone": ("Reporter phone", "notification"),
        "policy.insured_organisation": ("Insured organisation", "policy"),
        "policy.policy_type": ("Policy type", "policy"),
        "loss.injuries": ("Injuries", "loss"),
        "loss.fatalities": ("Fatalities", "loss"),
        "additional.incident_reference": ("Incident reference", "additional"),
        "additional.authorities_involved": ("Authorities involved", "additional"),
    }
)

#: Every section a field belongs to must have retrieval queries covering it, or a field
#: can be added that no search will ever find evidence for. Checked at import so the
#: omission is a startup failure rather than a quietly uncited field months later.
_UNCOVERED_SECTIONS = {section for _, section in _FIELD_META.values()} - set(SECTION_QUERIES)
if _UNCOVERED_SECTIONS:  # pragma: no cover — a developer error, caught at import
    raise RuntimeError(
        "These FNOL field sections have no retrieval queries in "
        f"app.services.fnol.evidence.SECTION_QUERIES: {sorted(_UNCOVERED_SECTIONS)}"
    )


@dataclass(slots=True)
class ExtractionOutcome:
    extraction: FNOLExtraction | None
    provider: str
    model: str | None
    latency_ms: int
    fingerprint: str
    error: str | None = None
    #: The passages the model was shown, if any. Carried on the outcome rather than
    #: returned separately so `apply` cannot be handed a different set than the one
    #: the model actually read — which would resolve every label to the wrong passage.
    evidence: EvidenceBundle | None = None

    @property
    def succeeded(self) -> bool:
        return self.extraction is not None

    @property
    def retrieval_used(self) -> bool:
        return bool(self.evidence and self.evidence.used)


class FNOLExtractionService:
    """Turns a notification and its documents into validated fields."""

    def __init__(
        self,
        repository: FNOLRepository,
        *,
        provider: AIProvider | None,
        ai_config: AISettings | None = None,
        evidence: FNOLEvidenceService | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._ai = ai_config or settings.ai
        #: `None` means no retrieval — the whole corpus is sent, exactly as it was
        #: before this existed. Retrieval narrows the prompt; it never gates it.
        self._evidence = evidence

    @staticmethod
    def fingerprint(source_text: str, document_texts: list[str], index_signature: str = "") -> str:
        """What the extraction depends on, hashed.

        The pipeline compares this against the stored fingerprint and skips the
        call when it has not moved. That is what stops a page refresh, a status
        change or an unrelated edit from costing a model call.

        `index_signature` folds in the state of the case's passages, so re-indexing —
        a new attachment, a re-read after OCR was switched on, a chunk-size change —
        moves the fingerprint and the notice is re-read. Without it a case would keep
        an extraction that cites passages which no longer exist.

        The two-argument form is kept working for callers that have no passages.
        """
        digest = hashlib.sha256()
        digest.update(source_text.encode("utf-8", errors="ignore"))
        for text in sorted(document_texts):
            digest.update(b"\x00")
            digest.update(text.encode("utf-8", errors="ignore"))
        if index_signature:
            digest.update(b"\x00")
            digest.update(index_signature.encode("utf-8"))
        return digest.hexdigest()

    async def extract(
        self,
        *,
        source_text: str,
        document_texts: list[str],
        channel: str,
        case_id: uuid.UUID | None = None,
        documents: list[Any] | None = None,
        index_signature: str = "",
    ) -> ExtractionOutcome:
        """Read the notice, falling back to the deterministic reader on failure."""
        fingerprint = self.fingerprint(source_text, document_texts, index_signature)
        bundle = await self._gather(case_id, documents)
        corpus = self._compose(source_text, document_texts, bundle)

        if self._provider is None:
            return self._heuristic(corpus, fingerprint, channel)

        try:
            response = await self._provider.structured(
                schema=FNOLExtraction,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=self._user_prompt(corpus, channel),
                schema_name="fnol_extraction",
            )
        except AIProviderError as exc:
            logger.warning("fnol_extraction_provider_failed", error=str(exc))
            outcome = self._heuristic(corpus, fingerprint, channel)
            outcome.error = (
                f"The model provider could not be reached ({exc}); the notice was read "
                "with the deterministic extractor instead."
            )
            return outcome

        return ExtractionOutcome(
            extraction=response.data,
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            fingerprint=fingerprint,
            evidence=bundle,
        )

    async def _gather(
        self, case_id: uuid.UUID | None, documents: list[Any] | None
    ) -> EvidenceBundle | None:
        """Retrieve passages, or `None` when retrieval is not in play."""
        if self._evidence is None or case_id is None:
            return None
        try:
            return await self._evidence.gather(case_id, documents or [])
        except Exception as exc:
            # Retrieval failing must not fail the extraction. The whole corpus is
            # still there and is what the pipeline read before retrieval existed.
            logger.warning("fnol_evidence_unavailable", error=type(exc).__name__)
            return None

    def _compose(
        self, source_text: str, document_texts: list[str], bundle: EvidenceBundle | None
    ) -> str:
        """The prompt body: the notice, then either retrieved passages or everything.

        The notification body is always first and always whole. It is small, it is the
        notice itself, and a retrieval miss on it would be the one failure this whole
        mechanism must not be able to cause.
        """
        if bundle is not None and bundle.used:
            budget = self._ai.max_input_characters
            head = source_text.strip()[:budget]
            remaining = max(0, budget - len(head))
            return f"{head}\n\n{bundle.prompt[:remaining]}"
        return self._build_corpus(source_text, document_texts)

    def _heuristic(self, corpus: str, fingerprint: str, channel: str) -> ExtractionOutcome:
        del channel
        return ExtractionOutcome(
            extraction=extract_from_text(corpus),
            provider=HEURISTIC_PROVIDER,
            model=None,
            latency_ms=0,
            fingerprint=fingerprint,
        )

    def _build_corpus(self, source_text: str, document_texts: list[str]) -> str:
        """The notice plus its attachments, budgeted.

        Each document is truncated in turn rather than the whole corpus being cut
        at the end: a 40-page schedule attached first must not push the broker's
        actual email out of the prompt.
        """
        budget = self._ai.max_input_characters
        parts = [source_text.strip()]
        remaining = max(0, budget - len(parts[0]))

        for index, text in enumerate(document_texts, start=1):
            if remaining <= 0:
                break
            share = min(len(text), max(2000, remaining // max(1, len(document_texts))))
            parts.append(f"\n\n--- ATTACHMENT {index} ---\n{text[:share]}")
            remaining -= share

        return "".join(parts)

    def _user_prompt(self, corpus: str, channel: str) -> str:
        return (
            f"Notification channel: {channel}.\n"
            "Extract the FNOL fields from the notification below.\n\n"
            "=== NOTIFICATION ===\n"
            f"{corpus}\n"
            "=== END ==="
        )

    async def apply(
        self,
        case: Any,
        extraction: FNOLExtraction,
        *,
        evidence: EvidenceBundle | None = None,
    ) -> dict[str, float | None]:
        """Write the extraction onto the case and its provenance rows.

        Returns the confidence of each field path, which the completeness engine
        uses to mark a value as uncertain rather than present.

        Values are only written to the case where the case does not already carry
        a human-supplied one — the field row is the authority on that, and this is
        the second place the rule is enforced, because losing an officer's
        correction is the one bug that must not be possible.

        `evidence` is the passage set the model was shown. Each field's
        `source_chunk_ref` is resolved against it so the review screen can point at
        the page a value is written on. A label that resolves to nothing is dropped:
        the *value* is still stored, because a model may read a value correctly and
        mislabel where it came from, but the citation is not — provenance pointing at
        a passage that was never shown is worse than none.
        """
        confidences: dict[str, float | None] = {}
        unresolved: set[str] = set()

        async def record(
            path: str, field: ExtractedField, *, parsed: object, attribute: str | None
        ) -> None:
            label, section = _FIELD_META.get(
                path, (path.split(".")[-1].replace("_", " ").title(), path.split(".")[0])
            )

            citation = evidence.resolve(field.source_chunk_ref) if evidence else None
            if field.source_chunk_ref and citation is None:
                unresolved.add(field.source_chunk_ref)

            row = await self._repository.upsert_field(
                case.id,
                field_path=path,
                section=section,
                label=label,
                value_text=field.value,
                confidence=field.confidence if field.present else None,
                source=FieldSource.AI,
                source_document_id=citation.document_id if citation else None,
                source_chunk_id=citation.chunk_id if citation else None,
                evidence_snippet=field.evidence,
            )
            confidences[path] = row.confidence

            if attribute and parsed is not None and not row.human_modified:
                setattr(case, attribute, parsed)

        notification = extraction.notification
        await record(
            "notification.reporter_name",
            notification.reporter_name,
            parsed=normalisation.clip(notification.reporter_name.value, 255),
            attribute="reporter_name",
        )
        await record(
            "notification.reporter_organisation",
            notification.reporter_organisation,
            parsed=normalisation.clip(notification.reporter_organisation.value, 255),
            attribute="reporter_organisation",
        )
        await record(
            "notification.reporter_role",
            notification.reporter_role,
            parsed=normalisation.clip(notification.reporter_role.value, 96),
            attribute="reporter_role",
        )
        await record(
            "notification.reporter_email",
            notification.reporter_email,
            parsed=normalisation.parse_email(notification.reporter_email.value),
            attribute="reporter_email",
        )
        await record(
            "notification.reporter_phone",
            notification.reporter_phone,
            parsed=normalisation.parse_phone(notification.reporter_phone.value),
            attribute="reporter_phone",
        )

        policy = extraction.policy
        await record(
            "policy.policy_number",
            policy.policy_number,
            parsed=normalisation.clip(policy.policy_number.value, 64),
            attribute="policy_number",
        )
        await record(
            "policy.insured_name",
            policy.insured_name,
            parsed=normalisation.clip(policy.insured_name.value, 255),
            attribute="insured_name",
        )
        await record(
            "policy.insured_organisation",
            policy.insured_organisation,
            parsed=normalisation.clip(policy.insured_organisation.value, 255),
            attribute="insured_organisation",
        )
        await record(
            "policy.policy_type",
            policy.policy_type,
            parsed=normalisation.clip(policy.policy_type.value, 64),
            attribute="policy_type",
        )

        loss = extraction.loss
        await record(
            "loss.date_of_loss",
            loss.date_of_loss,
            parsed=normalisation.parse_datetime(
                loss.date_of_loss.value,
                loss.time_of_loss.value,
                # The notice's own arrival, so a loss the model read as "yesterday
                # afternoon" lands on the case here exactly as it does on the
                # dataset path. Without it the words resolve against today, which
                # on a notice processed a week late is a week wrong.
                reference=getattr(case, "received_at", None),
            ),
            attribute="date_of_loss",
        )
        await record(
            "loss.loss_location",
            loss.loss_location,
            parsed=normalisation.clip(loss.loss_location.value, 2000),
            attribute="loss_location",
        )
        await record(
            "loss.loss_country",
            loss.loss_country,
            parsed=normalisation.clip(loss.loss_country.value, 64),
            attribute="loss_country",
        )
        await record(
            "loss.loss_description",
            loss.loss_description,
            parsed=normalisation.clip(loss.loss_description.value, 4000),
            attribute="loss_description",
        )
        await record(
            "loss.cause_of_loss",
            loss.cause_of_loss,
            parsed=normalisation.clip(loss.cause_of_loss.value, 255),
            attribute="cause_of_loss",
        )
        await record(
            "loss.affected_assets",
            loss.affected_assets,
            parsed=normalisation.clip(loss.affected_assets.value, 2000),
            attribute="affected_assets",
        )
        await record(
            "loss.injuries",
            loss.injuries,
            parsed=normalisation.parse_count(loss.injuries.value),
            attribute="injuries",
        )
        await record(
            "loss.fatalities",
            loss.fatalities,
            parsed=normalisation.parse_count(loss.fatalities.value),
            attribute="fatalities",
        )

        currency = normalisation.parse_currency(
            loss.currency.value or loss.estimated_loss_amount.value,
            fallback=case.currency or settings.fnol.base_currency,
        )
        if not _is_human_set(
            case,
            "financial.estimated_loss",
            await self._repository.get_field(case.id, "financial.estimated_loss"),
        ):
            case.currency = currency

        await record(
            "financial.estimated_loss",
            loss.estimated_loss_amount,
            parsed=normalisation.parse_money_minor(loss.estimated_loss_amount.value),
            attribute="estimated_loss_minor",
        )

        additional = extraction.additional
        await record(
            "financial.repair_estimate",
            additional.repair_estimate_amount,
            parsed=normalisation.parse_money_minor(additional.repair_estimate_amount.value),
            attribute="repair_estimate_minor",
        )
        await record(
            "additional.police_reference",
            additional.police_reference,
            parsed=normalisation.clip(additional.police_reference.value, 128),
            attribute="police_reference",
        )
        await record(
            "additional.incident_reference",
            additional.incident_reference,
            parsed=normalisation.clip(additional.incident_reference.value, 128),
            attribute="incident_reference",
        )
        await record(
            "additional.authorities_involved",
            additional.authorities_involved,
            parsed=normalisation.clip(additional.authorities_involved.value, 2000),
            attribute="authorities_involved",
        )

        for attribute, field in (
            ("business_interruption", loss.business_interruption),
            ("structural_damage", loss.structural_damage),
            ("environmental_exposure", loss.environmental_exposure),
            ("potential_litigation", additional.potential_litigation),
        ):
            parsed = normalisation.parse_bool(field.value)
            if parsed is not None:
                setattr(case, attribute, parsed)

        for party in extraction.parties:
            if not party.name.strip():
                continue
            await self._repository.upsert_party(
                case.id,
                role=normalisation.parse_party_role(party.role),
                name=normalisation.clip(party.name, 255) or party.name,
                organisation=normalisation.clip(party.organisation, 255),
                email=normalisation.parse_email(party.email),
                phone=normalisation.parse_phone(party.phone),
                source=FieldSource.AI,
                confidence=party.confidence,
            )

        if unresolved:
            # Same posture as the classification service's rejection of a
            # hallucinated line of business: reported, not stored, not fatal.
            logger.warning(
                "fnol_extraction_unknown_chunk_refs",
                reference=getattr(case, "reference", None),
                labels=sorted(unresolved),
            )

        case.extraction_confidence = extraction.overall_confidence
        return confidences


def _is_human_set(case: Any, path: str, row: Any) -> bool:
    del case, path
    return bool(row is not None and row.human_modified)


#: Paths this service knows how to write back onto the case. Exposed so the edit
#: endpoint and the completeness engine agree on what is editable.
EDITABLE_FIELD_PATHS = frozenset(FIELD_READERS)
