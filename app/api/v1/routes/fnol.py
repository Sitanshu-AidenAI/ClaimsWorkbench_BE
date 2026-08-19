"""FNOL intake endpoints.

Two things shape this router. First, **one call per user intention**: the review
workspace reads a whole case in one request and the command centre reads the
whole board in one request, because a screen assembled from nine calls shows nine
different moments of the same queue. Second, **the route is a thin edge** — it
authorises, validates, delegates to a service, maps the result and commits. Every
rule it appears to enforce actually lives in `app.services.fnol`.

Authorisation is enforced here on the server, not only in the client's routing:
`FNOL_WRITE_ROLES` gates every mutation, and reads are open to the wider claims
population because a handler working a claim needs the notice behind it.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.api.deps.auth import require_roles
from app.api.deps.services import FNOLContext, FNOLContextDep
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain import policy_identification as engine
from app.domain.enums import (
    FNOL_DELETE_ROLES,
    FNOL_READ_ROLES,
    FNOL_WRITE_ROLES,
    AnalysisKind,
    AuditEventType,
    DocumentIndexStatus,
    DuplicateResolution,
    ExceptionStatus,
    FNOLChannel,
    FNOLStatus,
    PolicyIdentificationStatus,
    Severity,
    SignalOutcome,
)
from app.domain.lifecycle import BLOCKING_EXCEPTIONS, can_transition
from app.domain.normalisation import parse_line_of_business
from app.domain.rules import valid_loss_type
from app.models.fnol import FNOLCase
from app.schemas import fnol as api
from app.services.fnol.ingestion import (
    IncomingAttachment,
    IncomingEmail,
    IncomingNotification,
)
from app.services.intelligence.highlight import (
    locate_in_chunk,
    page_for_offset,
    resolve_pdf_rects,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/fnol", tags=["fnol"])

ReadAccess = Annotated[Principal, Depends(require_roles(*FNOL_READ_ROLES))]
WriteAccess = Annotated[Principal, Depends(require_roles(*FNOL_WRITE_ROLES))]
#: Destroying a notice is a supervisor's call. See `FNOL_DELETE_ROLES`.
DeleteAccess = Annotated[Principal, Depends(require_roles(*FNOL_DELETE_ROLES))]

#: The three the board's insight column counts, in the order it draws them.
_INSIGHT_CODES = (
    ("major_losses", "high_severity", "potential major losses"),
    ("duplicates", "possible_duplicate", "possible duplicates"),
    ("missing_policy", "no_policy_match", "without a policy match"),
    ("cat_linked", "cat_match", "linked to a catastrophe event"),
)


def actor_of(principal: Principal) -> str:
    """How a person is named in the audit trail: their name, not their subject."""
    return principal.full_name or principal.username or principal.email or principal.subject


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=api.FNOLDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Log a notification from a structured channel",
)
async def create_notification(
    payload: api.NotificationCreate,
    context: FNOLContextDep,
    principal: WriteAccess,
    idempotency_key: Annotated[str | None, Query(alias="idempotency_key")] = None,
) -> api.FNOLDetail:
    """Portal, API, phone, TPA and manual entry all land here.

    An `idempotency_key` makes the call safe to retry: the same key returns the
    same notice rather than logging a second one, which matters most on the phone
    channel where an officer's browser is the thing that timed out.
    """
    notification = IncomingNotification(
        channel=payload.channel,
        received_at=payload.received_at or datetime.now(UTC),
        body=payload.body,
        source_metadata=payload.source_metadata,
        reporter_name=payload.reporter_name,
        reporter_organisation=payload.reporter_organisation,
        reporter_role=payload.reporter_role,
        reporter_email=payload.reporter_email,
        reporter_phone=payload.reporter_phone,
        external_reference=payload.external_reference,
        source_reference=payload.source_reference,
        idempotency_key=idempotency_key,
        supplied_fields=_supplied(payload.supplied),
    )

    case, created = await context.ingestion.ingest(notification, actor=actor_of(principal))
    if created:
        await context.ingestion.record_supplied_provenance(case, notification)
        if payload.process:
            await context.pipeline.run(case)
        else:
            context.fnol.mark_queued(case)

    await context.commit()
    return await build_detail(context, case)


@router.post(
    "/email",
    response_model=api.FNOLDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest an inbound claim email",
)
async def ingest_email(
    payload: api.EmailNotificationCreate,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    """The mailbox integration's entry point.

    Redelivery is normal for email — servers retry, and brokers reply-all onto the
    same thread — so a message id that has been seen before returns the existing
    notice rather than creating a second one.
    """
    attachments = [
        IncomingAttachment(
            filename=item.filename,
            content=_decode_attachment(item.filename, item.content_base64),
            content_type=item.content_type,
        )
        for item in payload.attachments
    ]

    email = IncomingEmail(
        sender=payload.sender,
        recipient=payload.recipient,
        subject=payload.subject,
        body=payload.body,
        message_id=payload.message_id,
        received_at=payload.received_at,
        thread_id=payload.thread_id,
        cc=payload.cc,
        headers=payload.headers,
        attachments=attachments,
        from_broker=payload.from_broker,
        body_is_html=payload.body_is_html,
    )

    notification = context.ingestion.from_email(email)
    case, created = await context.ingestion.ingest(notification, actor=actor_of(principal))

    if created:
        for attachment in attachments:
            await context.fnol.attach_document(
                case,
                filename=attachment.filename,
                content=attachment.content,
                content_type=attachment.content_type,
                source="email_attachment",
                actor=actor_of(principal),
            )
        if payload.process:
            await context.pipeline.run(case)
        else:
            context.fnol.mark_queued(case)

    await context.commit()
    return await build_detail(context, case)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=api.FNOLListResult, summary="List notifications")
async def list_notifications(
    context: FNOLContextDep,
    principal: ReadAccess,
    status_filter: Annotated[list[FNOLStatus] | None, Query(alias="status")] = None,
    channel: Annotated[list[FNOLChannel] | None, Query()] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    needs_review: Annotated[bool, Query()] = False,
    unassigned: Annotated[bool, Query()] = False,
    order: Annotated[str, Query(pattern="^(received_at|severity)$")] = "received_at",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> api.FNOLListResult:
    del principal
    rows, total = await context.cases.list_cases(
        statuses=[value.value for value in status_filter] if status_filter else None,
        channels=[value.value for value in channel] if channel else None,
        severities=[value.value for value in severity] if severity else None,
        search=search,
        needs_review=needs_review,
        unassigned=unassigned,
        order=order,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return api.FNOLListResult(
        items=[await _summarise(context, case) for case in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/board", response_model=api.BoardResult, summary="The intake command centre")
async def board(
    context: FNOLContextDep,
    principal: ReadAccess,
    order: Annotated[str, Query(pattern="^(received_at|severity)$")] = "severity",
    unassigned: Annotated[bool, Query()] = False,
    blocking: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
    page: Annotated[int, Query(ge=1)] = 1,
) -> api.BoardResult:
    """Figures, queue and insights in one call.

    One call rather than three: the band, the queue and the insight counts all
    describe the same moment of the same book, and fetching them separately is how
    a board ends up saying 127 above a list of 131.

    The queue is a page of that book rather than the top of it. `limit` is the page
    size and is left where it was so existing callers keep the slice they asked
    for; `page` walks it. The figures, insights and counts describe the whole book
    on every page — they are the board's read of the morning, not of the eight
    rows currently on screen.
    """
    del principal
    metrics = await context.fnol.board_metrics()
    rows, total = await context.cases.list_cases(
        order=order,
        unassigned=unassigned,
        blocking=blocking,
        limit=limit,
        offset=(page - 1) * limit,
    )

    processed = sum(
        count
        for status_value, count in metrics.status_counts.items()
        if status_value != FNOLStatus.RECEIVED
    )
    total_cases = sum(metrics.status_counts.values())
    with_policy = total_cases - metrics.exception_counts.get("no_policy_match", 0)
    converted = metrics.status_counts.get(FNOLStatus.CLAIM_CREATED, 0)

    return api.BoardResult(
        items=[await _summarise(context, case) for case in rows],
        total=total,
        page=page,
        page_size=limit,
        metrics=[
            api.BoardMetric(
                id="new_intake",
                label="New intake",
                value=metrics.received_today,
                note="received in the last 24 hours",
            ),
            api.BoardMetric(
                id="needs_review",
                label="Needs review",
                value=metrics.needs_review,
                note="waiting on an intake officer",
            ),
            api.BoardMetric(
                id="high_severity",
                label="High severity",
                value=metrics.high_severity,
                note="assessed high or critical",
            ),
            api.BoardMetric(
                id="exceptions",
                label="Open exceptions",
                value=metrics.exceptions_open,
                note="notifications with something outstanding",
            ),
        ],
        insights=[
            api.BoardInsight(
                id=insight_id,
                count=metrics.exception_counts.get(code, 0),
                label=label,
            )
            for insight_id, code, label in _INSIGHT_CODES
        ],
        ingest=[
            api.IngestStageOut(
                id="classified", label="Read and classified", done=processed, total=total_cases
            ),
            api.IngestStageOut(
                id="policy_matched",
                label="Policy matched",
                done=max(0, with_policy),
                total=total_cases,
            ),
            api.IngestStageOut(
                id="converted", label="Converted to claims", done=converted, total=total_cases
            ),
        ],
        status_counts=metrics.status_counts,
        channel_counts=metrics.channel_counts,
        captured_at=datetime.now(UTC),
    )


@router.get("/{reference}", response_model=api.FNOLDetail, summary="One notification in full")
async def get_notification(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> api.FNOLDetail:
    del principal
    case = await context.fnol.get(reference)
    return await build_detail(context, case)


@router.get(
    "/{reference}/audit",
    response_model=list[api.AuditEventOut],
    summary="The notification's audit trail",
)
async def audit_history(
    reference: str, context: FNOLContextDep, principal: ReadAccess
) -> list[api.AuditEventOut]:
    del principal
    case = await context.fnol.get(reference)
    related = (case.claim_id,) if case.claim_id else ()
    events = await context.audit.history(case.id, related_ids=related)
    return [api.to_audit_event(event) for event in events]


@router.delete(
    "/{reference}",
    response_model=api.FNOLDeletionResult,
    summary="Delete a notification and everything held against it",
)
async def delete_notification(
    reference: str,
    context: FNOLContextDep,
    principal: DeleteAccess,
    reason: Annotated[str | None, Query(max_length=500)] = None,
) -> api.FNOLDeletionResult:
    """Remove the notice from every store that holds any part of it.

    That is the notice and its nine child tables, the passages read out of its
    documents, the dataset values extracted from them, the attachment bytes in
    object storage, the vectors in the search index, and the mailbox ledger rows
    that collected it. The audit trail is the one thing kept — including a final
    event naming who did this and why.

    **Not the same as rejecting or cancelling.** Those are lifecycle decisions and
    they keep the record, which is almost always what an officer wanting a notice
    "off the queue" actually needs. This is for a notice that should never have
    existed: a test submission, a misfiled email, a duplicate ingested twice. It
    is refused outright once a claim has been created, because the claim is then
    the record and would be left citing nothing.

    Restricted to supervisors — see `FNOL_DELETE_ROLES`.
    """
    case = await context.fnol.get(reference)
    receipt = await context.deletion.remove(case, actor=actor_of(principal), reason=reason)

    # Committed before the stores are swept, and the ordering is the design — see
    # the module docstring on `app.services.fnol.deletion`.
    await context.commit()
    await context.deletion.purge_stores(receipt)

    return api.FNOLDeletionResult(
        reference=receipt.reference,
        deleted=True,
        records=receipt.records,
        total_records=receipt.total_records,
        documents_stored=len(receipt.storage_keys),
        blobs_removed=receipt.blobs_removed,
        vectors_cleared=receipt.vectors_cleared,
        warnings=receipt.warnings,
    )


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


@router.post(
    "/{reference}/process",
    response_model=api.ProcessResult,
    summary="Run the intake pipeline",
)
async def process(
    reference: str,
    payload: api.ProcessRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.ProcessResult:
    """Read, classify, match, assess and raise exceptions, in one operation.

    A single orchestration endpoint rather than one per stage: the stages feed
    each other, and a client driving them individually would be re-implementing
    the pipeline over the network — and could stop halfway.
    """
    del principal
    case = await context.fnol.get(reference)
    result = await context.pipeline.run(case, force=payload.force)
    await context.commit()

    return api.ProcessResult(
        reference=case.reference,
        status=result.status,
        processing_state=case.processing_state,
        extraction_reused=result.extraction_reused,
        exceptions_raised=result.exceptions_raised,
        error=result.error,
        documents_indexed=result.documents_indexed,
        chunks_indexed=result.chunks_indexed,
        retrieval_used=result.retrieval_used,
    )


# ---------------------------------------------------------------------------
# Document passages and evidence
# ---------------------------------------------------------------------------


@router.post(
    "/{reference}/index",
    response_model=api.DocumentIndexResult,
    summary="Read the attached documents into citable passages",
)
async def index_documents(
    reference: str,
    payload: api.DocumentIndexRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.DocumentIndexResult:
    """Cut the case's documents into passages, and vectorise them if configured.

    Ordinarily this happens on its own: uploading a document or collecting an email
    queues the notice, and the worker indexes it. This endpoint is the manual handle —
    for a case whose documents predate indexing, or for `force: true` after a
    configuration change that means they should be read differently.

    Runs inline rather than enqueueing, because a caller who asked for this wants the
    answer. The fingerprint guard means the common case is a query per document.
    """
    del principal
    case = await context.fnol.get(reference)

    documents = list(await context.cases.list_documents(case.id))
    if payload.document_id is not None:
        documents = [document for document in documents if document.id == payload.document_id]
        if not documents:
            raise NotFoundError("That document is not attached to this notification.")

    outcome = await context.index.index_case(documents, force=payload.force)
    if payload.run_pipeline:
        # New passages move the extraction fingerprint, so this genuinely re-reads.
        await context.pipeline.run(case, force=payload.force)
    await context.commit()

    return api.DocumentIndexResult(
        reference=case.reference,
        processing_state=case.processing_state,
        documents=len(outcome.documents),
        indexed=outcome.indexed,
        skipped=sum(
            1 for document in outcome.documents if document.status == DocumentIndexStatus.SKIPPED
        ),
        failed=outcome.failed,
        reused=sum(1 for document in outcome.documents if document.reused),
        chunks=outcome.chunks,
        embedded=outcome.embedded,
        errors=[document.error for document in outcome.documents if document.error],
    )


@router.get(
    "/{reference}/documents/{document_id}/chunks",
    response_model=api.DocumentChunkListResult,
    summary="List a document's passages",
)
async def list_document_chunks(
    reference: str,
    document_id: uuid.UUID,
    context: FNOLContextDep,
    principal: ReadAccess,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> api.DocumentChunkListResult:
    """The passages a document was cut into, in order.

    Paginated because a long survey report is hundreds of passages, and this is a
    diagnostic view — it answers "what did the reader actually get out of this file".
    """
    del principal
    case = await context.fnol.get(reference)
    document = await _document_of(context, case, document_id)

    total = await context.chunks.count_for_document(document.id)
    rows = await context.chunks.list_for_document(
        document.id, offset=(page - 1) * page_size, limit=page_size
    )
    return api.DocumentChunkListResult(
        items=[api.to_chunk(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{reference}/search",
    response_model=api.DocumentSearchResult,
    summary="Search the attached documents",
)
async def search_documents(
    reference: str,
    context: FNOLContextDep,
    principal: ReadAccess,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    top_k: Annotated[int, Query(ge=1, le=50)] = 10,
    document_id: Annotated[uuid.UUID | None, Query()] = None,
) -> api.DocumentSearchResult:
    """Find the passages of this notice's documents that answer a question.

    Scoped to one case always. There is no cross-case document search here on purpose:
    the passages of one claim are not evidence in another, and an endpoint that could
    return them would be a disclosure risk rather than a feature.
    """
    del principal
    case = await context.fnol.get(reference)

    result = await context.retrieval.search(case.id, q, limit=top_k, document_id=document_id)
    filenames = {
        document.id: document.filename for document in await context.cases.list_documents(case.id)
    }
    return api.DocumentSearchResult(
        items=[
            api.to_search_hit(hit, filename=filenames.get(hit.chunk.fnol_document_id, "attachment"))
            for hit in result.hits
        ],
        strategy=result.strategy,
        degraded=result.degraded,
        chunks_searched=result.chunks_available,
    )


@router.get(
    "/{reference}/fields/{field_path}/evidence",
    response_model=api.FieldEvidenceOut,
    summary="Show where a field's value came from",
)
async def field_evidence(
    reference: str,
    field_path: str,
    context: FNOLContextDep,
    principal: ReadAccess,
) -> api.FieldEvidenceOut:
    """The document, page and text behind one extracted value.

    This is what the review screen calls when an officer clicks a field. It answers at
    whatever precision the document allows and says which it reached: the page and the
    exact text always, plus rectangles when the document is a PDF whose words could be
    located. A field with no citation is not an error — a value typed by an officer, or
    read from the email body, has no passage to point at.
    """
    del principal
    case = await context.fnol.get(reference)

    field = await context.cases.get_field(case.id, field_path)
    if field is None:
        raise NotFoundError(f"No field '{field_path}' has been recorded on this notification.")

    chunk = (
        await context.chunks.get(field.source_chunk_id)
        if field.source_chunk_id is not None
        else None
    )
    document = None
    if chunk is not None:
        document = await context.cases.get_document(chunk.fnol_document_id)
    elif field.source_document_id is not None:
        document = await context.cases.get_document(field.source_document_id)

    base = api.FieldEvidenceOut(
        path=field.field_path,
        label=field.label,
        value=field.value_text,
        confidence=float(field.confidence) if field.confidence is not None else None,
        source=field.source,
        human_modified=bool(field.human_modified),
        document_id=document.id if document else None,
        filename=document.filename if document else None,
        content_type=document.content_type if document else None,
        chunk_id=chunk.id if chunk else None,
        chunk_ref=chunk.chunk_ref if chunk else None,
        page_number=chunk.page_number if chunk else None,
        section_label=chunk.section_label if chunk else None,
        text=field.evidence_snippet,
        char_start=None,
        char_end=None,
        rects=[],
        note=None
        if chunk is not None
        else "This value has no document passage recorded against it.",
    )

    if chunk is None or document is None:
        return base

    start, end, text = locate_in_chunk(chunk.content, chunk.char_start, field.evidence_snippet)
    located = page_for_offset(document.page_offsets, start)
    page_number = (located[0] + 1) if located else chunk.page_number

    rects: list[api.HighlightRectOut] = []
    note: str | None = None
    if document.content_type == "application/pdf" and located is not None:
        try:
            content = await context.documents.fetch(document.storage_key)
        except Exception:
            note = "The stored file could not be read to draw the highlight."
        else:
            resolved, note = resolve_pdf_rects(content, page_index=located[0], text=text)
            rects = [api.to_highlight_rect(rect) for rect in resolved]
    elif document.content_type != "application/pdf":
        # A Word document or a spreadsheet has no page geometry to draw on. The page
        # label and the exact text are the honest answer, and are enough for a text
        # viewer to highlight in.
        note = "This document has no page layout; highlight the quoted text instead."

    return base.model_copy(
        update={
            "page_number": page_number,
            "text": text,
            "char_start": start,
            "char_end": end,
            "rects": rects,
            "note": note,
        }
    )


def _enqueue_index(case: FNOLCase) -> None:
    """Ask a worker to index and process this notice.

    Called *after* the commit, deliberately: a task that starts before the transaction
    lands would read a case that does not exist yet. Failing to enqueue is logged and
    not raised — the notice is already `queued`, and the beat poller is the safety net
    that makes the queue eventually-consistent rather than dependent on this call.
    """
    try:
        from app.workers.tasks import index_case_documents

        index_case_documents.delay(str(case.id))
    except Exception as exc:
        logger.warning(
            "index_enqueue_failed",
            reference=case.reference,
            error=type(exc).__name__,
        )


async def _field_citations(
    context: FNOLContext, fields: Any, documents: Any
) -> dict[uuid.UUID, tuple[str, int | None]]:
    """`chunk_id -> (filename, page_number)` for every field that cites a passage.

    One query for the whole screen. The filename comes from the case's documents,
    already loaded, so this costs a single passage lookup and no joins.
    """
    ids = [field.source_chunk_id for field in fields if field.source_chunk_id is not None]
    if not ids:
        return {}

    filenames = {document.id: document.filename for document in documents}
    resolved: dict[uuid.UUID, tuple[str, int | None]] = {}
    for chunk_id in set(ids):
        chunk = await context.chunks.get(chunk_id)
        if chunk is None:
            # The passage was replaced by a re-index. The value stands; the citation
            # does not, and saying nothing is better than naming the wrong page.
            continue
        resolved[chunk_id] = (
            filenames.get(chunk.fnol_document_id, "attachment"),
            chunk.page_number,
        )
    return resolved


async def _document_of(context: FNOLContext, case: FNOLCase, document_id: uuid.UUID) -> Any:
    """One of this case's documents, or a 404.

    Scoped through the case rather than fetched by id alone: a document id from
    another notice must not resolve here.
    """
    document = await context.cases.get_document(document_id)
    if document is None or document.fnol_case_id != case.id:
        raise NotFoundError("That document is not attached to this notification.")
    return document


async def _identification_view(
    context: FNOLContext, case: FNOLCase
) -> api.PolicyIdentificationOut:
    """Assemble the identification stage's whole answer.

    The near misses are read out of the stored analysis and re-hydrated against the
    policy rows they name. A policy that has since been deleted from the book simply
    drops out rather than being reported as a candidate that cannot be opened — a
    stale explanation is worth less than a short one.
    """
    signals = engine.searched_on(await context.identification.notice_signals(case))
    stored = await context.cases.get_analysis(case.id, AnalysisKind.POLICY_IDENTIFICATION)
    payload: dict[str, Any] = (stored.result or {}) if stored is not None else {}

    matches = await context.cases.list_policy_matches(case.id)
    candidates: list[api.PolicyCandidateOut] = []
    for match in matches:
        policy = await context.policies.get(match.policy_id)
        if policy is not None:
            candidates.append(api.to_policy_candidate(match, policy))

    near_misses: list[api.PolicyCandidateOut] = []
    for entry in payload.get("near_misses", []) or []:
        candidate = await _near_miss(context, entry)
        if candidate is not None:
            near_misses.append(candidate)

    selected = next((match for match in matches if match.selected), None)
    present = [signal for signal in signals if signal.outcome is SignalOutcome.MATCH]

    return api.PolicyIdentificationOut(
        reference=case.reference,
        status=case.policy_identification_status,
        strength=str(payload.get("strength", "none")),
        ran_at=case.policy_identification_ran_at,
        engine_version=str(payload.get("engine_version") or engine.ENGINE_VERSION),
        policies_compared=int(payload.get("policies_compared", 0) or 0),
        extracted_signals=[_extracted_signal(signal) for signal in signals],
        signals_present=len(present),
        signals_missing=len(signals) - len(present),
        candidates=candidates,
        near_misses=near_misses,
        recommended_policy_id=_optional_uuid(payload.get("recommended_policy_id")),
        selected_policy_id=selected.policy_id if selected is not None else None,
        policy_confirmed=bool(case.policy_confirmed),
        confirmed_by=case.policy_confirmed_by,
        confirmed_at=case.policy_confirmed_at,
        referral_reason=case.policy_referral_reason,
        referred_by=case.policy_referred_by,
        decided=bool(case.policy_confirmed)
        or case.policy_identification_status == PolicyIdentificationStatus.REFERRED,
    )


async def _near_miss(context: FNOLContext, entry: dict[str, Any]) -> api.PolicyCandidateOut | None:
    """One below-threshold candidate, read back out of the stored analysis.

    Shown because an officer recognises the right policy at 0.3 far more often than
    the arithmetic does, and labelled by its `rejected` confidence so nothing about
    the row suggests the engine put it forward.
    """
    policy_id = _optional_uuid(entry.get("policy_id"))
    if policy_id is None:
        return None
    policy = await context.policies.get(policy_id)
    if policy is None:
        return None
    return api.to_policy_candidate(_StoredCandidate(policy_id, entry), policy)


class _StoredCandidate:
    """A candidate from the analysis blob, in the shape the row mapper reads.

    A shim rather than a second mapper: the near misses and the persisted candidates
    are the same thing at different stages of a decision, and drawing them through
    two code paths is how the two lists start disagreeing about what a score means.
    """

    __slots__ = (
        "confidence",
        "display",
        "engine_version",
        "match_strength",
        "matched_on",
        "origin",
        "period_outcome",
        "policy_id",
        "rank",
        "reasoning",
        "recommended",
        "score",
        "selected",
        "selected_at",
        "selected_by",
        "signals",
        "warnings",
    )

    def __init__(self, policy_id: uuid.UUID, entry: dict[str, Any]) -> None:
        self.policy_id = policy_id
        self.match_strength = str(entry.get("match_strength", "none"))
        self.confidence = str(entry.get("confidence", "rejected"))
        self.score = float(entry.get("score", 0.0) or 0.0)
        self.rank = int(entry.get("rank", 0) or 0)
        self.matched_on = entry.get("matched_on") or {}
        self.signals = entry.get("signals") or []
        self.warnings = entry.get("warnings") or []
        self.display = entry.get("display") or {}
        self.period_outcome = str(entry.get("period_outcome", "unknown"))
        self.reasoning = entry.get("reasoning")
        self.origin = "engine"
        self.recommended = False
        self.selected = False
        self.selected_by = None
        self.selected_at = None
        self.engine_version = entry.get("engine_version")


def _extracted_signal(signal: Any) -> api.ExtractedSignalOut:
    return api.ExtractedSignalOut(
        signal=signal.signal,
        label=signal.label,
        axis=signal.axis.value,
        outcome=signal.outcome.value,
        value=signal.notice_value,
        explanation=signal.explanation,
        confidence=signal.score,
        weight=signal.weight,
        evidence_field_key=signal.evidence_field_key,
        document_id=signal.document_id if isinstance(signal.document_id, uuid.UUID) else None,
        page_number=signal.page_number,
        quote=signal.quote,
    )


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Human review
# ---------------------------------------------------------------------------


@router.patch("/{reference}", response_model=api.FNOLDetail, summary="Correct extracted values")
async def update_fields(
    reference: str,
    payload: api.FieldUpdateRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    changed = await context.fnol.update_fields(
        case, payload.updates, actor=actor_of(principal), reason=payload.reason
    )
    if changed and payload.reprocess:
        # A corrected date of loss changes the policy match, the CAT match and
        # half the exceptions, so the notice is re-derived rather than left
        # showing conclusions drawn from the old value.
        await context.pipeline.run(case)
    elif changed:
        await context.fnol.refresh_status(case)

    await context.commit()
    return await build_detail(context, case)


# ---------------------------------------------------------------------------
# Policy identification
# ---------------------------------------------------------------------------


@router.get(
    "/{reference}/policy-identification",
    response_model=api.PolicyIdentificationOut,
    summary="Which policy this notice belongs to, and why",
)
async def policy_identification(
    reference: str,
    context: FNOLContextDep,
    principal: ReadAccess,
) -> api.PolicyIdentificationOut:
    """The whole first-stage answer in one call.

    Three things, and they are read from three places on purpose. The **signals**
    are rebuilt from the case every time, because an officer who has just corrected
    an insured name must see the corrected one. The **candidates** come from the
    rows an officer can act on, which is what makes the selection rule enforceable.
    The **near misses** come from the stored analysis, because they were compared
    and rejected — they are an explanation, not an option, until someone chooses one.
    """
    del principal
    case = await context.fnol.get(reference)
    return await _identification_view(context, case)


@router.post(
    "/{reference}/policy-identification",
    response_model=api.PolicyIdentificationOut,
    summary="Identify the policy again",
)
async def rerun_policy_identification(
    reference: str,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.PolicyIdentificationOut:
    """Re-run identification alone, without re-reading the notice.

    Its own endpoint rather than a flag on `/process` because the two answer
    different questions. Re-reading the notice costs a model call and moves every
    extracted value; re-identifying costs one query and a few hundred comparisons,
    and is what an officer wants after a policy has been loaded into the book or
    after they have corrected the insured's name.

    A notice that has already been confirmed is left alone: re-running would
    reshuffle candidates behind a decision somebody has already taken.
    """
    del principal
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    if case.policy_confirmed:
        raise ValidationError(
            "A policy has already been confirmed on this notification. Change the "
            "selection instead of re-running identification."
        )

    result = await context.identification.identify(case)
    await context.commit()
    logger.info(
        "fnol_policy_identification_rerun",
        reference=case.reference,
        candidates=len(result.candidates),
    )
    return await _identification_view(context, case)


@router.post(
    "/{reference}/policy-selection",
    response_model=api.FNOLDetail,
    summary="Confirm the policy this notice belongs to",
)
async def select_policy(
    reference: str,
    payload: api.PolicySelectionRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    """The officer's decision, recorded and made authoritative.

    The policy has to be a **record in the database**, and that is the whole of the
    safety rule: an id that does not resolve is refused, so no AI output and no
    mistyped request can attach a claim to a policy that does not exist. It does not
    have to be one the engine ranked — a policy an officer found in the book is
    scored and persisted as a candidate first, which keeps "the bound policy is one
    of this case's candidates" true while letting the search actually be used.

    The pipeline is re-run afterwards because confirming a policy changes the
    coverage read, the limit check and the exception list — a response that returned
    only the selection would leave five panels describing the case as it was.
    """
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    policy = await context.identification.confirm(
        case, payload.policy_id, actor=actor_of(principal), reason=payload.reason
    )
    if policy is None:
        raise ValidationError(
            "No policy with that identifier exists in the policy book. Search for the "
            "policy and select it from the results."
        )

    context.audit.fnol(
        case,
        event_type=AuditEventType.POLICY_SELECTED,
        summary=f"Policy {policy.policy_number} confirmed by {actor_of(principal)}.",
        actor=actor_of(principal),
        after={"policy_number": policy.policy_number},
        context={"reason": payload.reason} if payload.reason else {},
    )

    await context.pipeline.run(case)
    await context.commit()
    return await build_detail(context, case)


@router.post(
    "/{reference}/policy-referral",
    response_model=api.FNOLDetail,
    summary="Record that no policy could be identified",
)
async def refer_no_policy(
    reference: str,
    payload: api.PolicyReferralRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    """The honest ending when the book holds no answer.

    A decision, not a gap, which is why it takes a reason and why it moves the
    notice to `referred` rather than leaving it in the queue looking unworked. The
    next person to open it needs to know what was already tried.
    """
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    await context.identification.refer(case, actor=actor_of(principal), reason=payload.reason)

    context.audit.fnol(
        case,
        event_type=AuditEventType.POLICY_REFERRED,
        summary=f"No policy identified; referred by {actor_of(principal)}.",
        actor=actor_of(principal),
        context={"reason": payload.reason},
    )

    # Moved to `referred` when the state machine allows it. A notice that is
    # already terminal keeps its status: a referral of something that has become a
    # claim is refused above, and one that was cancelled is not worth reopening.
    if can_transition(FNOLStatus(case.status), FNOLStatus.REFERRED):
        await context.fnol.transition(
            case, FNOLStatus.REFERRED, actor=actor_of(principal), reason=payload.reason
        )
    await context.commit()
    return await build_detail(context, case)


@router.get(
    "/{reference}/policy-search",
    response_model=api.PolicySearchResult,
    summary="Search the policy book for this notice",
)
async def search_policies(
    reference: str,
    context: FNOLContextDep,
    principal: ReadAccess,
    q: Annotated[str, Query(min_length=2, max_length=120)],
) -> api.PolicySearchResult:
    """What an officer uses when the engine ranked the wrong policies.

    Results carry the same per-signal working the ranked candidates do, scored
    against this notice. That is the difference between a fallback and an
    afterthought: an officer comparing four policies they found by hand needs to see
    why each does and does not fit, or they are choosing blind.

    Nothing is persisted. Searching is looking; only confirming is deciding.
    """
    del principal
    case = await context.fnol.get(reference)
    scored = await context.identification.search(case, q)

    items: list[api.PolicyCandidateOut] = []
    for candidate in scored:
        policy = await context.policies.get(candidate.policy_id)
        if policy is not None:
            items.append(api.to_scored_candidate(candidate, policy))

    return api.PolicySearchResult(items=items, term=q, exhausted=not items)


@router.post(
    "/{reference}/duplicates/{duplicate_id}",
    response_model=api.FNOLDetail,
    summary="Resolve a possible duplicate",
)
async def resolve_duplicate(
    reference: str,
    duplicate_id: uuid.UUID,
    payload: api.DuplicateResolutionRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    await context.fnol.resolve_duplicate(
        case,
        duplicate_id,
        resolution=DuplicateResolution(payload.resolution),
        note=payload.note,
        actor=actor_of(principal),
    )
    await context.fnol.refresh_status(case)
    await context.commit()
    return await build_detail(context, case)


@router.post(
    "/{reference}/exceptions/{exception_id}",
    response_model=api.FNOLDetail,
    summary="Mark an exception dealt with",
)
async def resolve_exception(
    reference: str,
    exception_id: uuid.UUID,
    payload: api.ExceptionResolutionRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    await context.fnol.resolve_exception(
        case,
        exception_id,
        status=ExceptionStatus(payload.status),
        note=payload.note,
        actor=actor_of(principal),
    )
    await context.commit()
    return await build_detail(context, case)


@router.post(
    "/{reference}/severity", response_model=api.FNOLDetail, summary="Override the severity"
)
async def override_severity(
    reference: str,
    payload: api.SeverityOverrideRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)
    await context.fnol.override_severity(
        case, severity=payload.severity, reason=payload.reason, actor=actor_of(principal)
    )
    await context.commit()
    return await build_detail(context, case)


@router.post(
    "/{reference}/classification",
    response_model=api.FNOLDetail,
    summary="Override the classification",
)
async def override_classification(
    reference: str,
    payload: api.ClassificationOverrideRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    line = parse_line_of_business(payload.line_of_business)
    if line is None:
        raise ValidationError(
            f"“{payload.line_of_business}” is not a line of business this carrier writes."
        )
    loss_type = valid_loss_type(line, payload.loss_type) if payload.loss_type else None
    if payload.loss_type and loss_type is None:
        raise ValidationError(
            f"“{payload.loss_type}” is not a loss type recorded against "
            f"{line.value.replace('_', ' ')} claims."
        )

    await context.fnol.override_classification(
        case,
        line_of_business=line.value,
        loss_type=loss_type,
        claim_type=payload.claim_type,
        reason=payload.reason,
        actor=actor_of(principal),
    )
    await context.commit()
    return await build_detail(context, case)


@router.post(
    "/{reference}/cat-match",
    response_model=api.FNOLDetail,
    summary="Confirm or remove a catastrophe attribution",
)
async def confirm_cat_match(
    reference: str,
    payload: api.CatMatchRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)
    await context.fnol.confirm_cat_match(
        case,
        confirmed=payload.confirmed,
        event_id=payload.event_id,
        actor=actor_of(principal),
    )
    await context.fnol.refresh_status(case)
    await context.commit()
    return await build_detail(context, case)


@router.post("/{reference}/notes", response_model=api.NoteOut, summary="Add a note")
async def add_note(
    reference: str,
    payload: api.NoteRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.NoteOut:
    case = await context.fnol.get(reference)
    note = await context.fnol.add_note(case, body=payload.body, actor=actor_of(principal))
    await context.commit()
    return api.to_note(note)


@router.post("/{reference}/status", response_model=api.FNOLDetail, summary="Move the notification")
async def transition(
    reference: str,
    payload: api.TransitionRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    """Refer, reject or cancel a notification.

    Claim creation is deliberately not reachable from here — it has its own
    endpoint because it has its own preconditions and its own idempotency.
    """
    if payload.status is FNOLStatus.CLAIM_CREATED:
        raise ValidationError(
            "A claim is created through the create-claim action, not by setting a status."
        )
    case = await context.fnol.get(reference)
    await context.fnol.transition(
        case, payload.status, actor=actor_of(principal), reason=payload.reason
    )
    await context.commit()
    return await build_detail(context, case)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@router.post(
    "/{reference}/documents",
    response_model=api.FNOLDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a document",
)
async def upload_document(
    reference: str,
    context: FNOLContextDep,
    principal: WriteAccess,
    file: Annotated[UploadFile, File()],
    reprocess: Annotated[bool, Query()] = True,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    _refuse_if_converted(case)

    content = await file.read()
    await context.fnol.attach_document(
        case,
        filename=file.filename or "attachment",
        content=content,
        content_type=file.content_type,
        source="upload",
        actor=actor_of(principal),
    )
    if reprocess:
        # Queued rather than processed inline. Reading a document now means opening it,
        # possibly sending it to OCR, chunking it and embedding it — seconds to minutes
        # for a large scan, inside an HTTP request that a proxy will give up on first.
        # The worker picks the notice up from `queued`; the client polls
        # `GET /fnol/{reference}` and watches `processing_state`.
        context.fnol.mark_queued(case)

    await context.commit()
    _enqueue_index(case)
    return await build_detail(context, case)


@router.delete(
    "/{reference}/documents/{document_id}",
    response_model=api.FNOLDetail,
    summary="Remove a document",
)
async def delete_document(
    reference: str,
    document_id: uuid.UUID,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    case = await context.fnol.get(reference)
    await context.fnol.remove_document(case, document_id, actor=actor_of(principal))
    await context.pipeline.run(case)
    await context.commit()
    return await build_detail(context, case)


@router.get(
    "/{reference}/documents/{document_id}/content",
    summary="Download an attached document",
)
async def download_document(
    reference: str,
    document_id: uuid.UUID,
    context: FNOLContextDep,
    principal: ReadAccess,
) -> Response:
    del principal
    case = await context.fnol.get(reference)
    document = await context.cases.get_document(document_id)
    if document is None or document.fnol_case_id != case.id:
        raise NotFoundError("That document is not on this notification.")

    content = await context.documents.fetch(document.storage_key)
    return Response(
        content=content,
        media_type=document.content_type,
        headers={
            # `attachment` rather than `inline`: a claim document is arbitrary
            # user-supplied content, and rendering it in the application's origin
            # is how an uploaded file becomes a script on the claims desk.
            "Content-Disposition": f'attachment; filename="{document.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# Claim creation
# ---------------------------------------------------------------------------


@router.post(
    "/{reference}/create-claim",
    response_model=api.FNOLDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create the claim",
)
async def create_claim(
    reference: str,
    payload: api.ClaimCreationRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.FNOLDetail:
    """Convert a reviewed notification into a claim.

    Refuses with a checklist when something blocks it, and returns the existing
    claim — rather than making a second one — when the notice has already been
    converted.
    """
    case = await context.fnol.get(reference)
    result = await context.creation.create(
        case.id, actor=actor_of(principal), idempotency_key=payload.idempotency_key
    )
    await context.commit()

    logger.info(
        "fnol_claim_creation",
        fnol=case.reference,
        claim=result.claim.reference,
        created=result.created,
    )
    refreshed = await context.fnol.get(reference)
    return await build_detail(context, refreshed)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


async def _summarise(context: FNOLContext, case: FNOLCase) -> api.FNOLSummary:
    exceptions = await context.cases.list_exceptions(case.id, only_open=True)
    blocking = sum(
        1 for item in exceptions if item.code in {code.value for code in BLOCKING_EXCEPTIONS}
    )
    claim_reference = None
    if case.claim_id:
        claim = await context.claims.get(case.claim_id)
        claim_reference = claim.reference if claim else None

    return api.to_summary(
        case,
        open_exceptions=len(exceptions),
        blocking_exceptions=blocking,
        claim_reference=claim_reference,
    )


async def build_detail(context: FNOLContext, case: FNOLCase) -> api.FNOLDetail:
    """Assemble the whole review workspace payload.

    The case is refreshed first because `updated_at` is maintained by a database
    trigger: after any write, SQLAlchemy knows the column is stale and would
    reload it lazily — which, inside a response being serialised, is IO in a
    place the async driver cannot do it. One explicit SELECT here is both cheaper
    and considerably less mysterious.
    """
    await context.session.refresh(case)
    documents = await context.cases.list_documents(case.id)
    fields = await context.cases.list_fields(case.id)
    # Resolved in one query, not one per field: `FNOLDocumentChunk` is deliberately
    # `lazy="raise"`, so reading a citation through the relationship while serialising
    # a response would be an error rather than a slow success.
    citations = await _field_citations(context, fields, documents)
    parties = await context.cases.list_parties(case.id)
    matches = await context.cases.list_policy_matches(case.id)
    duplicates = await context.cases.list_duplicates(case.id)
    exceptions = await context.cases.list_exceptions(case.id)
    analyses = await context.cases.list_analyses(case.id)
    notes = await context.cases.list_notes(case.id)

    candidates: list[api.PolicyCandidateOut] = []
    for match in matches:
        policy = await context.policies.get(match.policy_id)
        if policy is not None:
            candidates.append(api.to_policy_candidate(match, policy))

    cat_event = None
    if case.cat_event_id:
        event = await context.cat_events.get(case.cat_event_id)
        if event is not None:
            cat_event = api.to_cat_event(
                event,
                confidence=float(case.cat_confidence) if case.cat_confidence else None,
                confirmed=bool(case.cat_confirmed),
            )

    claim_reference = None
    if case.claim_id:
        claim = await context.claims.get(case.claim_id)
        claim_reference = claim.reference if claim else None

    blockers = await context.creation.blockers(case)

    return api.FNOLDetail(
        id=case.id,
        reference=case.reference,
        title=api.case_title(case),
        status=FNOLStatus(case.status),
        processing_state=case.processing_state,
        processing_error=case.processing_error,
        processing_completed_at=case.processing_completed_at,
        source=api.SourceOut(
            channel=FNOLChannel(case.channel),
            received_at=case.received_at,
            message_id=case.message_id,
            thread_id=case.thread_id,
            external_reference=case.external_reference,
            source_reference=case.source_reference,
            metadata=case.source_metadata or {},
            body=case.source_body,
        ),
        reporter_name=case.reporter_name,
        reporter_organisation=case.reporter_organisation,
        reporter_role=case.reporter_role,
        reporter_email=case.reporter_email,
        reporter_phone=case.reporter_phone,
        policy_number=case.policy_number,
        insured_name=case.insured_name,
        insured_organisation=case.insured_organisation,
        policy_type=case.policy_type,
        policy_id=case.policy_id,
        policy_confirmed=bool(case.policy_confirmed),
        policy_identification_status=case.policy_identification_status,
        policy_referral_reason=case.policy_referral_reason,
        broker_name=case.broker_name,
        broker_reference=case.broker_reference,
        risk_location=case.risk_location,
        loss_postcode=case.loss_postcode,
        project_name=case.project_name,
        contract_number=case.contract_number,
        policy_period_stated=case.policy_period_stated,
        line_of_business=case.line_of_business,
        claim_type=case.claim_type,
        loss_type=case.loss_type,
        complexity=case.complexity,
        classification_confidence=_float(case.classification_confidence),
        classification_overridden=bool(case.classification_overridden),
        date_of_loss=case.date_of_loss,
        loss_location=case.loss_location,
        loss_country=case.loss_country,
        loss_description=case.loss_description,
        cause_of_loss=case.cause_of_loss,
        affected_assets=case.affected_assets,
        injuries=case.injuries,
        fatalities=case.fatalities,
        business_interruption=bool(case.business_interruption),
        structural_damage=bool(case.structural_damage),
        environmental_exposure=bool(case.environmental_exposure),
        estimated_loss=api.money(case.estimated_loss_minor, case.currency),
        repair_estimate=api.money(case.repair_estimate_minor, case.currency),
        currency=case.currency or "GBP",
        police_reference=case.police_reference,
        incident_reference=case.incident_reference,
        authorities_involved=case.authorities_involved,
        potential_litigation=bool(case.potential_litigation),
        severity=Severity(case.severity) if case.severity else None,
        severity_confidence=_float(case.severity_confidence),
        severity_overridden=bool(case.severity_overridden),
        fraud_risk=case.fraud_risk,
        fraud_score=_float(case.fraud_score),
        coverage_indicator=case.coverage_indicator,
        completeness_score=_float(case.completeness_score),
        extraction_confidence=_float(case.extraction_confidence),
        ai_summary=case.ai_summary,
        ai_summary_generated_at=case.ai_summary_generated_at,
        cat_event=cat_event,
        claim_reference=claim_reference,
        assigned_to=case.assigned_to,
        created_by=case.created_by,
        created_at=case.created_at,
        updated_at=case.updated_at,
        documents=[api.to_document(document) for document in documents],
        fields=[api.to_field(field, citations) for field in fields],
        parties=[api.to_party(party) for party in parties],
        policy_candidates=candidates,
        duplicates=[api.to_duplicate(candidate) for candidate in duplicates],
        exceptions=[api.to_exception(exception) for exception in exceptions],
        analyses={analysis.kind: api.to_analysis(analysis) for analysis in analyses},
        notes=[api.to_note(note) for note in notes],
        blockers=[
            api.BlockerOut(code=blocker.code, message=blocker.message) for blocker in blockers
        ],
        can_create_claim=not blockers and case.claim_id is None,
    )


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _supplied(supplied: dict[str, object]) -> dict[str, object]:
    """Only the attributes the case actually has. Anything else is dropped.

    A structured channel is trusted to *state* values, not to name columns: an
    unknown key here would be a client writing to a field this API never
    published.
    """
    allowed = {
        "reporter_name",
        "reporter_organisation",
        "reporter_role",
        "reporter_email",
        "reporter_phone",
        "policy_number",
        "insured_name",
        "insured_organisation",
        "loss_location",
        "loss_country",
        "loss_description",
        "cause_of_loss",
        "affected_assets",
        "police_reference",
        "incident_reference",
    }
    return {key: value for key, value in supplied.items() if key in allowed}


def _decode_attachment(filename: str, encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(
            f"The attachment {filename} was not valid base64.", details={"filename": filename}
        ) from exc


def _refuse_if_converted(case: FNOLCase) -> None:
    """A converted notice is a historical record and does not change.

    Corrections after conversion belong on the claim, which has its own trail —
    editing the notice would rewrite what the claim was created from.
    """
    if case.status == FNOLStatus.CLAIM_CREATED:
        raise ValidationError(
            f"{case.reference} has already been converted into a claim and cannot be edited. "
            "Make the correction on the claim instead."
        )
