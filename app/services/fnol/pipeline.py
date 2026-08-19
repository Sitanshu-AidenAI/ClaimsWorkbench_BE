"""The FNOL processing pipeline.

The order below is the order a claims officer would work in, and each stage feeds
the next: you cannot assess coverage before you have a policy, and you cannot
match a policy before you have read the notice. Written as one readable sequence
rather than buried in a controller, so the order is reviewable by someone who
knows claims and not Python.

    index documents → extract → classify → identify the policy → check completeness
    → detect duplicates → assess severity, fraud and coverage → match catastrophe
    → summarise → raise exceptions → set status

Indexing is stage zero and is what makes the rest citable: it cuts each document's
text into passages carrying the page they came from, so a field extracted afterwards
can point at where it was read. It is idempotent, so a re-run on an unchanged case
costs one query per document and nothing else.

Two economies are built in and both matter at production volume:

* **Nothing is recomputed while its inputs hold still.** The extraction and the
  summary each carry a fingerprint of what they were built from, and a re-run
  with an unchanged fingerprint skips the model call entirely.
* **One model call reads the whole notice.** Everything after extraction is
  deterministic code over the extracted values. When a case carries enough document
  text to be worth searching, that one call reads the passages retrieval selected
  rather than a truncated concatenation of everything — but it is still one call.

The pipeline never raises for a stage failure. An unreachable model provider, an
unreadable PDF or a policy repository returning nothing are all normal states of
a claims desk, and each becomes an exception on the notice rather than a 500 on
the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import ExtractionSettings, settings
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
from app.repositories.extraction import ExtractionSchemaRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.policy import PolicyRepository
from app.services.extraction.engine import RunOutcome, SchemaExtractionEngine
from app.services.extraction.registry import to_dataset
from app.services.fnol.adapter import FNOLWriteBackAdapter, WriteBackResult
from app.services.fnol.assessments import AssessmentServices
from app.services.fnol.audit import AuditService
from app.services.fnol.body import NotificationBodyDocumentService
from app.services.fnol.classification import ClassificationService
from app.services.fnol.exceptions import ExceptionService
from app.services.fnol.extraction import FNOLExtractionService
from app.services.fnol.identification import PolicyIdentificationService
from app.services.fnol.matching import (
    CatastropheMatchingService,
    DuplicateDetectionService,
)
from app.services.fnol.summary import FNOLSummaryService
from app.services.intelligence.indexing import CaseIndexOutcome, DocumentIndexService
from app.services.notifications.service import NotificationService

logger = get_logger(__name__)


@dataclass(slots=True)
class PipelineResult:
    case: FNOLCase
    status: FNOLStatus
    extraction_reused: bool
    exceptions_raised: int
    error: str | None = None
    #: What the indexing stage did, and whether the extraction read retrieved
    #: passages or the whole corpus. Surfaced so "did retrieval do anything on this
    #: notice" is answerable from the response rather than from the logs.
    documents_indexed: int = 0
    chunks_indexed: int = 0
    retrieval_used: bool = False
    #: The dataset that read this notice, and how it went. `None` means the
    #: pre-existing fixed-schema path ran instead — no dataset is configured, or
    #: `CWB_EXTRACT_SCHEMA_DRIVEN` is off.
    schema_key: str | None = None
    extraction_run_id: str | None = None
    fields_extracted: int = 0
    fields_needing_review: int = 0


@dataclass(slots=True)
class _DatasetExtraction:
    """The dataset path's result, in the shape the shared downstream needs."""

    outcome: RunOutcome
    write_back: WriteBackResult
    fingerprint: str
    failed: bool
    retrieval_used: bool


class FNOLPipeline:
    def __init__(
        self,
        *,
        repository: FNOLRepository,
        policies: PolicyRepository,
        extraction: FNOLExtractionService,
        classification: ClassificationService,
        identification: PolicyIdentificationService,
        duplicates: DuplicateDetectionService,
        catastrophe: CatastropheMatchingService,
        assessments: AssessmentServices,
        summary: FNOLSummaryService,
        exceptions: ExceptionService,
        audit: AuditService,
        #: The desk's notification panel. Optional in the same way and for the same
        #: reason `index` is: a deployment or a test without one processes notices
        #: exactly as it did before the panel existed. Emission never raises, so a
        #: present one cannot fail a run either.
        notifications: NotificationService | None = None,
        index: DocumentIndexService | None = None,
        #: The configurable-dataset path. All four are `None`-able together: a
        #: deployment without them runs exactly as it did before this existed,
        #: which is what keeps the pre-existing behaviour reachable and tested.
        schemas: ExtractionSchemaRepository | None = None,
        engine: SchemaExtractionEngine | None = None,
        adapter: FNOLWriteBackAdapter | None = None,
        body: NotificationBodyDocumentService | None = None,
        extraction_config: ExtractionSettings | None = None,
        #: Awaited once, immediately after the run has been announced and before
        #: any stage has run. The worker passes `session.commit`.
        #:
        #: Without it the "processing started" notification is invisible until the
        #: caller's single commit at the *end* of the run — so it arrives in the
        #: same instant as the completion it exists to precede, which is no use to
        #: anyone watching the bell. Everything written before this point is
        #: already durable-worthy on its own: the case is claimed, `processing`,
        #: and stamped with `processing_started_at`. A worker dying after it
        #: leaves exactly the state `release_stale_processing` is there to reap.
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._checkpoint = checkpoint
        self._notifications = notifications
        self._index = index
        self._schemas = schemas
        self._engine = engine
        self._adapter = adapter
        self._body = body
        self._extraction_config = extraction_config or settings.extraction
        self._repository = repository
        self._policies = policies
        self._extraction = extraction
        self._classification = classification
        self._identification = identification
        self._duplicates = duplicates
        self._catastrophe = catastrophe
        self._assessments = assessments
        self._summary = summary
        self._exceptions = exceptions
        self._audit = audit

    async def run(self, case: FNOLCase, *, force: bool = False) -> PipelineResult:
        """Process a notice end to end. `force` re-reads it even if nothing moved."""
        # Held in a local as well as on the case: it is the discriminator in this
        # run's notification keys, and the three events must agree on it. Reading
        # it back off the case at the end would work today and break the moment
        # anything in between touches the column.
        started_at = datetime.now(UTC)
        case.processing_state = ProcessingState.PROCESSING
        case.processing_started_at = started_at
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
        if self._notifications is not None:
            await self._notifications.fnol_processing_started(
                case_id=case.id,
                reference=case.reference,
                started_at=started_at,
                channel=case.channel,
            )

        # Make the announcement visible *now*, not when the run ends. The three
        # events keep their shared `started_at` discriminator either way, so the
        # dedupe keys still agree across the commit boundary.
        if self._checkpoint is not None:
            await self._checkpoint()

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
            if self._notifications is not None:
                await self._notifications.fnol_processing_failed(
                    case_id=case.id,
                    reference=case.reference,
                    started_at=started_at,
                    error=case.processing_error,
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
        if self._notifications is not None:
            await self._notifications.fnol_processing_succeeded(
                case_id=case.id,
                reference=case.reference,
                started_at=started_at,
                status=result.status.value,
                exceptions_raised=result.exceptions_raised,
                fields_extracted=result.fields_extracted,
                documents_indexed=result.documents_indexed,
            )
        return result

    async def _run_stages(self, case: FNOLCase, *, force: bool) -> PipelineResult:
        # --- 0a. The notification body becomes a document ---------------------
        # So that a value read out of the broker's own email can be cited like a
        # value read out of an attachment. Everything downstream then treats the
        # body as an ordinary document, which is the whole point: no stage below
        # this line knows it is different.
        if self._body is not None:
            await self._body.ensure(case)

        documents = list(await self._repository.list_documents(case.id))

        # --- 0b. Index the documents ------------------------------------------
        # Turns stored text into citable passages, and vectorises them when a
        # provider is configured. Never raises: a document that cannot be read or
        # embedded becomes a failed row and is counted as unreadable below, which
        # feeds the exception the officer already sees for that.
        index = (
            await self._index.index_case(documents, force=force)
            if self._index is not None
            else CaseIndexOutcome.disabled()
        )

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
        # Two paths, and exactly one runs. The dataset path reads the notice
        # against whatever fields the desk has configured; the fixed path reads
        # it against the schema compiled into `app.domain.extraction`. The
        # dataset path is preferred when one is configured and the module is on,
        # and the fixed path remains the answer for a deployment that has neither
        # — including every existing test of the pre-existing behaviour.
        dataset = await self._extract_dataset(case, documents, index.signature, force=force)
        if dataset is not None:
            return await self._downstream(
                case,
                document_texts=document_texts,
                unreadable=unreadable,
                index=index,
                fingerprint=dataset.fingerprint,
                extraction_failed=dataset.failed,
                field_confidences=dataset.write_back.confidences,
                raw_loss_date=dataset.write_back.raw_loss_date,
                retrieval_used=dataset.retrieval_used,
                extraction_reused=dataset.outcome.reused,
                dataset=dataset,
            )

        fingerprint = FNOLExtractionService.fingerprint(
            case.source_body or "", document_texts, index.signature
        )
        stored = await self._repository.get_analysis(case.id, AnalysisKind.EXTRACTION)
        reused = bool(
            stored is not None
            and stored.input_fingerprint == fingerprint
            and stored.status == "completed"
            and not force
        )

        extraction_failed = False
        retrieval_used = False
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
                case_id=case.id,
                documents=documents,
                index_signature=index.signature,
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
                field_confidences = await self._extraction.apply(
                    case, outcome.extraction, evidence=outcome.evidence
                )
                raw_loss_date = outcome.extraction.loss.date_of_loss.value
                retrieval_used = outcome.retrieval_used
                await self._repository.record_analysis(
                    case.id,
                    kind=AnalysisKind.EXTRACTION,
                    provider=outcome.provider,
                    model=outcome.model,
                    input_fingerprint=fingerprint,
                    # The retrieval trace rides along in the same JSONB column rather
                    # than in a table of its own. It is the answer to "why did the
                    # model read that", it is already fingerprinted with the
                    # extraction it explains, and it is already served with it.
                    result={
                        **outcome.extraction.model_dump(mode="json"),
                        "retrieval": (outcome.evidence.trace if outcome.evidence else {}),
                    },
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

        return await self._downstream(
            case,
            document_texts=document_texts,
            unreadable=unreadable,
            index=index,
            fingerprint=fingerprint,
            extraction_failed=extraction_failed,
            field_confidences=field_confidences,
            raw_loss_date=raw_loss_date,
            retrieval_used=retrieval_used,
            extraction_reused=reused,
            dataset=None,
        )

    async def _downstream(
        self,
        case: FNOLCase,
        *,
        document_texts: list[str],
        unreadable: int,
        index: CaseIndexOutcome,
        fingerprint: str,
        extraction_failed: bool,
        field_confidences: dict[str, float | None],
        raw_loss_date: str | None,
        retrieval_used: bool,
        extraction_reused: bool,
        dataset: _DatasetExtraction | None,
    ) -> PipelineResult:
        """Everything after the notice has been read.

        Shared by both extraction paths, and unchanged from the sequence that
        came before them: classify, match, assess, summarise, raise exceptions,
        set status. Neither path may have its own version of this — the whole
        value of switching how a notice is *read* rests on nothing else about how
        it is *handled* changing with it.
        """
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

        # --- 3. Policy identification ---------------------------------------
        # The first decision on the notice, and the one the rest of the pipeline
        # depends on: coverage, the limit check and the claim all reason against a
        # policy. The whole answer is stored — the signals read off the notice, the
        # ranked candidates and the near misses below the line — because "why is my
        # policy not in the list" has to be answerable, and only the candidates an
        # officer can *act* on become rows.
        identification = await self._identification.identify(case)
        await self._repository.record_analysis(
            case.id,
            kind=AnalysisKind.POLICY_IDENTIFICATION,
            provider="deterministic",
            model=None,
            input_fingerprint=fingerprint,
            result=identification.as_dict(),
            confidence=identification.best.score if identification.best else None,
        )
        # The best candidate stands in for the bound policy while the match is
        # unconfirmed, so coverage can say "review required" rather than "no
        # policy located" about a notice that plainly has one. Nothing treats it
        # as authoritative: `policy_confirmed` is what the coverage verdict and
        # the exception engine read.
        policy = await self._resolve_policy(case, identification)

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
            policy_strength=identification.strength,
            policy_candidates=len(identification.candidates),
            identity_conflict=_identity_conflict(identification),
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
            extraction_reused=extraction_reused,
            exceptions_raised=len(raised),
            documents_indexed=index.indexed,
            chunks_indexed=index.chunks,
            retrieval_used=retrieval_used,
            schema_key=dataset.outcome.run.schema_key if dataset else None,
            extraction_run_id=str(dataset.outcome.run.id) if dataset else None,
            fields_extracted=dataset.outcome.run.fields_extracted if dataset else 0,
            fields_needing_review=dataset.outcome.run.fields_needing_review if dataset else 0,
        )

    # -- The configurable-dataset path ---------------------------------------

    async def write_back(self, case: FNOLCase, values: list[Any]) -> WriteBackResult | None:
        """Mirror extracted values onto the claim record, outside a full run.

        What the correction endpoint calls after an officer edits a value: the
        edit has to reach `fnol_cases` and `fnol_extracted_fields` in the same
        transaction, or the review screen and the pipeline disagree about what
        the value is until the next run. Exposed on the pipeline rather than the
        adapter so a route never has to know the adapter exists.
        """
        if self._adapter is None:
            return None
        return await self._adapter.apply(case, values)

    async def _extract_dataset(
        self,
        case: FNOLCase,
        documents: list[Any],
        index_signature: str,
        *,
        force: bool,
    ) -> _DatasetExtraction | None:
        """Read the notice against the configured dataset.

        Returns `None` — meaning "the fixed schema should run instead" — for four
        reasons, none of which is an error:

        * the module is switched off;
        * its collaborators were not wired;
        * **no model provider is configured**, so the deterministic reader is the
          only thing that can read anything, and it answers the fixed schema;
        * no dataset is configured, because nobody has set one up yet.
        """
        if not self._extraction_config.schema_driven:
            return None
        if self._schemas is None or self._engine is None or self._adapter is None:
            return None
        if not self._engine.available:
            # A desk with no API key keeps the deterministic reading it has always
            # had. A dataset run here would be honest and useless: it would
            # correctly report having read nothing, and the notice would land in
            # `ai_processing_failed` with an empty form where it used to carry
            # values the regex reader found.
            logger.info("fnol_dataset_needs_a_provider", reference=case.reference)
            return None

        schema = await self._schemas.get_default()
        if schema is None:
            logger.info("fnol_no_default_dataset", reference=case.reference)
            return None

        dataset = to_dataset(schema)
        outcome = await self._engine.run(
            case,
            schema=schema,
            dataset=dataset,
            documents=documents,
            index_signature=index_signature,
            force=force,
        )

        # The write-back runs on a reused run too. It is cheap — it reads rows
        # this transaction already has — and it is what guarantees the claim
        # record matches the values on screen after a restart, a schema rename or
        # a correction made through a different route.
        write_back = await self._adapter.apply(case, outcome.values)

        await self._repository.record_analysis(
            case.id,
            kind=AnalysisKind.EXTRACTION,
            provider=outcome.run.provider or "none",
            model=outcome.run.model,
            input_fingerprint=outcome.run.fingerprint or "",
            result={
                "schema_key": outcome.run.schema_key,
                "schema_version": outcome.run.schema_version,
                "run_id": str(outcome.run.id),
                "fields_total": outcome.run.fields_total,
                "fields_extracted": outcome.run.fields_extracted,
                "fields_needing_review": outcome.run.fields_needing_review,
                "retrieval": {
                    "strategy": outcome.run.retrieval_strategy,
                    "degraded": outcome.run.degraded,
                    "chunks_available": outcome.run.chunks_available,
                    "llm_calls": outcome.run.llm_calls,
                },
                "values": {
                    value.field_key: {
                        "value": value.value_text,
                        "confidence": float(value.confidence)
                        if value.confidence is not None
                        else None,
                        "needs_review": value.needs_review,
                        "chunk_id": str(value.source_chunk_id) if value.source_chunk_id else None,
                    }
                    for value in outcome.values
                },
            },
            status="completed" if outcome.succeeded else "failed",
            confidence=case.extraction_confidence,
            error=outcome.run.error,
            latency_ms=outcome.run.latency_ms,
        )

        if outcome.succeeded and not outcome.reused:
            self._audit.system(
                case,
                event_type=AuditEventType.EXTRACTION_COMPLETED,
                summary=(
                    f"Notification read against the {outcome.run.schema_key} dataset: "
                    f"{outcome.run.fields_extracted} of {outcome.run.fields_total} fields found, "
                    f"{outcome.run.fields_needing_review} needing review."
                ),
                context={
                    "schema": outcome.run.schema_key,
                    "provider": outcome.run.provider,
                    "model": outcome.run.model,
                },
            )

        return _DatasetExtraction(
            outcome=outcome,
            write_back=write_back,
            fingerprint=outcome.run.fingerprint or "",
            # A run that produced nothing at all is an extraction failure in the
            # sense the exception engine means: the notice could not be read, and
            # the officer should be told rather than shown an empty form.
            failed=not outcome.succeeded,
            retrieval_used=outcome.run.retrieval_strategy not in (None, "none", "whole-corpus"),
        )

    async def _resolve_policy(self, case: FNOLCase, identification: Any) -> Any | None:
        """The policy the rest of the pipeline reasons against.

        The bound policy when there is one, otherwise the strongest candidate.
        Reasoning against a candidate is what lets the coverage indicator and the
        policy-period exception say something useful about a notice whose match
        has not been confirmed — and `case.policy_confirmed` stays false, so
        nothing downstream mistakes it for a decision.
        """
        if case.policy_id:
            return await self._policies.get(case.policy_id)
        if identification.best is not None:
            return await self._policies.get(identification.best.policy_id)
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


def _identity_conflict(identification: Any) -> bool:
    """The top candidate's reference agrees and its insured name does not.

    Raised as its own exception rather than folded into "unconfirmed match",
    because the remedy is different: not "choose a policy" but "check this notice
    against the schedule before binding anything". It is the shape of a broker
    quoting a reference off the wrong covering schedule, and it is precisely the
    case where binding automatically would attach a claim to the wrong client.
    """
    best = identification.best
    if best is None:
        return False
    return any(
        warning.code == "identity_conflict" for warning in best.warnings
    )


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
