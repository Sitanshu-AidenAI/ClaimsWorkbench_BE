"""The FNOL processing pipeline.

The order below is the order a claims officer would work in, and each stage feeds
the next: you cannot assess coverage before you have a policy, and you cannot
match a policy before you have read the notice. Written as one readable sequence
rather than buried in a controller, so the order is reviewable by someone who
knows claims and not Python.

    read documents → extract → classify → match policy → check completeness
    → detect duplicates → assess severity, fraud and coverage → match catastrophe
    → summarise → raise exceptions → set status

Two economies are built in and both matter at production volume:

* **Nothing is recomputed while its inputs hold still.** The extraction and the
  summary each carry a fingerprint of what they were built from, and a re-run
  with an unchanged fingerprint skips the model call entirely.
* **One model call reads the whole notice.** Everything after extraction is
  deterministic code over the extracted values.

The pipeline never raises for a stage failure. An unreachable model provider, an
unreadable PDF or a policy repository returning nothing are all normal states of
a claims desk, and each becomes an exception on the notice rather than a 500 on
the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.domain.enums import (
    AnalysisKind,
    AuditEventType,
    DocumentExtractionStatus,
    ExceptionStatus,
    FNOLStatus,
    LineOfBusiness,
    ProcessingState,
)
from app.domain.lifecycle import derive_status
from app.models.fnol import FNOLCase
from app.repositories.fnol import FNOLRepository
from app.repositories.policy import PolicyRepository
from app.services.fnol.assessments import AssessmentServices
from app.services.fnol.audit import AuditService
from app.services.fnol.classification import ClassificationService
from app.services.fnol.exceptions import ExceptionService
from app.services.fnol.extraction import FNOLExtractionService
from app.services.fnol.matching import (
    CatastropheMatchingService,
    DuplicateDetectionService,
    PolicyMatchingService,
)
from app.services.fnol.summary import FNOLSummaryService

logger = get_logger(__name__)


@dataclass(slots=True)
class PipelineResult:
    case: FNOLCase
    status: FNOLStatus
    extraction_reused: bool
    exceptions_raised: int
    error: str | None = None


class FNOLPipeline:
    def __init__(
        self,
        *,
        repository: FNOLRepository,
        policies: PolicyRepository,
        extraction: FNOLExtractionService,
        classification: ClassificationService,
        policy_matching: PolicyMatchingService,
        duplicates: DuplicateDetectionService,
        catastrophe: CatastropheMatchingService,
        assessments: AssessmentServices,
        summary: FNOLSummaryService,
        exceptions: ExceptionService,
        audit: AuditService,
    ) -> None:
        self._repository = repository
        self._policies = policies
        self._extraction = extraction
        self._classification = classification
        self._policy_matching = policy_matching
        self._duplicates = duplicates
        self._catastrophe = catastrophe
        self._assessments = assessments
        self._summary = summary
        self._exceptions = exceptions
        self._audit = audit

    async def run(self, case: FNOLCase, *, force: bool = False) -> PipelineResult:
        """Process a notice end to end. `force` re-reads it even if nothing moved."""
        case.processing_state = ProcessingState.PROCESSING
        case.processing_started_at = datetime.now(UTC)
        case.processing_error = None
        if case.status == FNOLStatus.RECEIVED:
            case.status = FNOLStatus.PROCESSING
        await self._repository.flush()

        self._audit.system(
            case,
            event_type=AuditEventType.PIPELINE_STARTED,
            summary="Automated processing started.",
            context={"force": force},
        )

        try:
            result = await self._run_stages(case, force=force)
        except Exception as exc:
            logger.exception("fnol_pipeline_failed", reference=case.reference)
            case.processing_state = ProcessingState.FAILED
            case.processing_error = f"{type(exc).__name__}: {exc}"
            case.processing_completed_at = datetime.now(UTC)
            case.status = FNOLStatus.AI_PROCESSING_FAILED
            self._audit.system(
                case,
                event_type=AuditEventType.PIPELINE_FAILED,
                summary="Automated processing failed and needs to be retried.",
                context={"error": type(exc).__name__},
            )
            return PipelineResult(
                case=case,
                status=FNOLStatus.AI_PROCESSING_FAILED,
                extraction_reused=False,
                exceptions_raised=0,
                error=case.processing_error,
            )

        case.processing_state = ProcessingState.COMPLETED
        case.processing_completed_at = datetime.now(UTC)

        self._audit.system(
            case,
            event_type=AuditEventType.PIPELINE_COMPLETED,
            summary=(
                f"Automated processing completed: {result.exceptions_raised} item(s) "
                "need attention."
            ),
            context={
                "status": result.status.value,
                "completeness": float(case.completeness_score or 0),
                "extraction_reused": result.extraction_reused,
            },
        )
        return result

    async def _run_stages(self, case: FNOLCase, *, force: bool) -> PipelineResult:
        documents = list(await self._repository.list_documents(case.id))
        document_texts = [
            document.extracted_text for document in documents if document.extracted_text
        ]
        unreadable = sum(
            1
            for document in documents
            if document.extraction_status
            in (DocumentExtractionStatus.FAILED, DocumentExtractionStatus.UNSUPPORTED)
        )

        # --- 1. Extraction --------------------------------------------------
        fingerprint = FNOLExtractionService.fingerprint(case.source_body or "", document_texts)
        stored = await self._repository.get_analysis(case.id, AnalysisKind.EXTRACTION)
        reused = bool(
            stored is not None
            and stored.input_fingerprint == fingerprint
            and stored.status == "completed"
            and not force
        )

        extraction_failed = False
        field_confidences: dict[str, float | None] = {}
        raw_loss_date: str | None = None

        if reused:
            logger.info("fnol_extraction_reused", reference=case.reference)
            field_confidences = {
                field.field_path: field.confidence
                for field in await self._repository.list_fields(case.id)
            }
            raw_loss_date = _stored_raw_loss_date(stored)
        else:
            outcome = await self._extraction.extract(
                source_text=case.source_body or "",
                document_texts=document_texts,
                channel=case.channel,
            )
            if outcome.extraction is None:
                extraction_failed = True
                await self._repository.record_analysis(
                    case.id,
                    kind=AnalysisKind.EXTRACTION,
                    provider=outcome.provider,
                    model=outcome.model,
                    input_fingerprint=fingerprint,
                    result={},
                    status="failed",
                    error=outcome.error,
                    latency_ms=outcome.latency_ms,
                )
            else:
                field_confidences = await self._extraction.apply(case, outcome.extraction)
                raw_loss_date = outcome.extraction.loss.date_of_loss.value
                await self._repository.record_analysis(
                    case.id,
                    kind=AnalysisKind.EXTRACTION,
                    provider=outcome.provider,
                    model=outcome.model,
                    input_fingerprint=fingerprint,
                    result=outcome.extraction.model_dump(mode="json"),
                    confidence=outcome.extraction.overall_confidence,
                    error=outcome.error,
                    latency_ms=outcome.latency_ms,
                )
                self._audit.system(
                    case,
                    event_type=AuditEventType.EXTRACTION_COMPLETED,
                    summary=(
                        f"Notification read by {outcome.provider} with "
                        f"{outcome.extraction.overall_confidence:.0%} overall confidence."
                    ),
                    context={"provider": outcome.provider, "model": outcome.model},
                )

        # --- 2. Classification ----------------------------------------------
        corpus = "\n\n".join(part for part in (case.source_body or "", *document_texts) if part)[
            :20_000
        ]

        if not case.classification_overridden:
            classification = await self._classification.classify(
                text=corpus, policy_line=await self._policy_line(case)
            )
            case.line_of_business = classification.line_of_business.value
            case.claim_type = classification.claim_type
            case.loss_type = classification.loss_type
            case.complexity = classification.complexity
            case.classification_confidence = classification.confidence
            await self._repository.record_analysis(
                case.id,
                kind=AnalysisKind.CLASSIFICATION,
                provider=classification.provider,
                model=classification.model,
                input_fingerprint=fingerprint,
                result=classification.as_dict(),
                confidence=classification.confidence,
            )

        # --- 3. Policy matching ---------------------------------------------
        policy_outcome = await self._policy_matching.match(case)
        await self._repository.record_analysis(
            case.id,
            kind=AnalysisKind.POLICY_MATCH,
            provider="deterministic",
            model=None,
            input_fingerprint=fingerprint,
            result=policy_outcome.as_dict(),
            confidence=policy_outcome.best.score if policy_outcome.best else None,
        )
        # The best candidate stands in for the bound policy while the match is
        # unconfirmed, so coverage can say "review required" rather than "no
        # policy located" about a notice that plainly has one. Nothing treats it
        # as authoritative: `policy_confirmed` is what the coverage verdict and
        # the exception engine read.
        policy = await self._resolve_policy(case, policy_outcome)

        # --- 4. Duplicates ---------------------------------------------------
        duplicate_outcome = await self._duplicates.detect(case)
        await self._repository.record_analysis(
            case.id,
            kind=AnalysisKind.DUPLICATES,
            provider="deterministic",
            model=None,
            input_fingerprint=fingerprint,
            result=duplicate_outcome.as_dict(),
            confidence=duplicate_outcome.strongest.score if duplicate_outcome.strongest else None,
        )

        # --- 5. Catastrophe --------------------------------------------------
        cat_outcome = await self._catastrophe.match(case)
        await self._repository.record_analysis(
            case.id,
            kind=AnalysisKind.CATASTROPHE,
            provider="deterministic",
            model=None,
            input_fingerprint=fingerprint,
            result=cat_outcome.as_dict(),
            confidence=cat_outcome.best.confidence if cat_outcome.best else None,
        )

        # --- 6. Assessments ---------------------------------------------------
        # Flushed before reading back: the session runs with autoflush off, so
        # rows the matching stages have only added would be invisible to the
        # queries below — and the case would be assessed against an empty
        # duplicate list it had just populated.
        await self._repository.flush()
        duplicate_rows = list(await self._repository.list_duplicates(case.id))
        unresolved_duplicates = [row for row in duplicate_rows if row.resolution == "unresolved"]
        assessment = await self._assessments.run(
            case,
            policy=policy,
            field_confidences=field_confidences or None,
            duplicate_count=len(unresolved_duplicates),
            cat_matched=case.cat_event_id is not None,
        )
        for kind, payload, confidence in (
            (
                AnalysisKind.COMPLETENESS,
                assessment.completeness.as_dict(),
                assessment.completeness.score,
            ),
            (AnalysisKind.SEVERITY, assessment.severity.as_dict(), assessment.severity.confidence),
            (AnalysisKind.FRAUD, assessment.fraud.as_dict(), assessment.fraud.score),
            (AnalysisKind.COVERAGE, assessment.coverage.as_dict(), assessment.coverage.confidence),
        ):
            await self._repository.record_analysis(
                case.id,
                kind=kind,
                provider="deterministic",
                model=None,
                input_fingerprint=fingerprint,
                result=payload,
                confidence=confidence,
            )

        # --- 7. Summary -------------------------------------------------------
        await self._summarise(case, policy=policy, assessment=assessment, cat=cat_outcome.best)

        # --- 8. Exceptions ----------------------------------------------------
        raised = await self._exceptions.evaluate(
            case,
            policy_strength=policy_outcome.strength,
            policy_candidates=len(policy_outcome.candidates),
            policy=policy,
            duplicates=duplicate_rows,
            completeness=assessment.completeness,
            severity=assessment.severity,
            fraud=assessment.fraud,
            coverage=assessment.coverage,
            cat_match=cat_outcome.best,
            extraction_failed=extraction_failed,
            extraction_confidence=case.extraction_confidence,
            unreadable_documents=unreadable,
            raw_loss_date=raw_loss_date,
        )

        # --- 9. Status --------------------------------------------------------
        await self._repository.flush()
        open_codes = [
            exception.code
            for exception in await self._repository.list_exceptions(case.id, only_open=True)
        ]
        case.status = derive_status(
            FNOLStatus(case.status),
            open_exception_codes=open_codes,
            extraction_failed=extraction_failed,
        )

        return PipelineResult(
            case=case,
            status=FNOLStatus(case.status),
            extraction_reused=reused,
            exceptions_raised=len(raised),
        )

    async def _resolve_policy(self, case: FNOLCase, outcome: Any) -> Any | None:
        """The policy the rest of the pipeline reasons against.

        The bound policy when there is one, otherwise the strongest candidate.
        Reasoning against a candidate is what lets the coverage indicator and the
        policy-period exception say something useful about a notice whose match
        has not been confirmed — and `case.policy_confirmed` stays false, so
        nothing downstream mistakes it for a decision.
        """
        if case.policy_id:
            return await self._policies.get(case.policy_id)
        if outcome.best is not None:
            return await self._policies.get(outcome.best.policy_id)
        return None

    async def _policy_line(self, case: FNOLCase) -> LineOfBusiness | None:
        if not case.policy_id:
            return None
        policy = await self._policies.get(case.policy_id)
        if policy is None:
            return None
        try:
            return LineOfBusiness(policy.line_of_business)
        except ValueError:
            return None

    async def _summarise(
        self, case: FNOLCase, *, policy: Any | None, assessment: Any, cat: Any | None
    ) -> None:
        """Rewrite the executive paragraph, but only if its facts moved."""
        facts = _summary_facts(case, policy=policy, assessment=assessment, cat=cat)
        fingerprint = FNOLSummaryService.fingerprint(facts)

        stored = await self._repository.get_analysis(case.id, AnalysisKind.SUMMARY)
        if stored is not None and stored.input_fingerprint == fingerprint and case.ai_summary:
            return

        outcome = await self._summary.summarise(facts)
        case.ai_summary = outcome.summary
        case.ai_summary_generated_at = datetime.now(UTC)
        await self._repository.record_analysis(
            case.id,
            kind=AnalysisKind.SUMMARY,
            provider=outcome.provider,
            model=outcome.model,
            input_fingerprint=fingerprint,
            result={"summary": outcome.summary, "key_points": outcome.key_points},
        )


def _summary_facts(
    case: FNOLCase, *, policy: Any | None, assessment: Any, cat: Any | None
) -> dict[str, Any]:
    """The facts the summary is allowed to state.

    Assembled here rather than in the summary service so the fingerprint and the
    prompt cannot drift apart — the summary is regenerated exactly when one of
    these values changes.
    """
    money = (
        f"{case.currency} {case.estimated_loss_minor / 100:,.0f}"
        if case.estimated_loss_minor
        else None
    )
    missing = [status.label for status in assessment.completeness.missing[:4]]

    return {
        "loss_summary": (case.loss_description or "").strip()[:400] or None,
        "reported_by": " ".join(
            part
            for part in (
                case.reporter_name,
                f"({case.reporter_organisation})" if case.reporter_organisation else "",
            )
            if part
        ).strip()
        or None,
        "insured": case.insured_name or case.insured_organisation,
        "policy_number": case.policy_number,
        "loss_location": case.loss_location,
        "date_of_loss": case.date_of_loss.strftime("%d %B %Y") if case.date_of_loss else None,
        "estimated_loss": money,
        "line_of_business": case.line_of_business,
        "policy_status": (
            f"The matched policy {policy.policy_number} ran "
            f"{policy.effective_date:%d %b %Y} to {policy.expiry_date:%d %b %Y}."
            if policy
            else "No policy has been matched to the notification."
        ),
        "cat_event": f"{cat.reference} ({cat.name})" if cat else None,
        "casualties": _casualties(case.injuries, case.fatalities),
        "completeness": (
            f"Documentation is {assessment.completeness.score:.0%} complete"
            + (f"; still missing {', '.join(missing)}." if missing else ".")
        ),
        "severity": assessment.severity.severity.value,
        "coverage": assessment.coverage.indicator.value,
    }


def _casualties(injuries: int | None, fatalities: int | None) -> str:
    """The human toll, stated in the grammar a person would use.

    "1 injuries" on the front page of a claims file is the kind of small
    carelessness that makes a reader distrust everything under it.
    """
    parts: list[str] = []
    if fatalities:
        parts.append(f"{fatalities} {'fatality' if fatalities == 1 else 'fatalities'}")
    if injuries:
        parts.append(f"{injuries} {'person' if injuries == 1 else 'people'} injured")

    if not parts:
        if injuries == 0 and fatalities == 0:
            return "No injuries or fatalities were reported."
        return "No injuries reported."
    return f"{' and '.join(parts)}."


def _stored_raw_loss_date(analysis: Any) -> str | None:
    try:
        return analysis.result["loss"]["date_of_loss"]["value"]
    except (KeyError, TypeError):
        return None


def open_exception_count(exceptions: list[Any]) -> int:
    return sum(1 for exception in exceptions if exception.status == ExceptionStatus.OPEN)
