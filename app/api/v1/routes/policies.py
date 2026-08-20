"""Policy library endpoints.

Two jobs on one router, and they are the two halves of the same feature: get a
carrier's wordings into the index, and ask the index which of them answers a notice.

The router is a **thin edge**, like every other one here: it authorises, validates,
delegates to a service, maps the result and commits. Every rule it appears to enforce
lives in `app/services/policies/`.

Two things shape the surface.

**Uploading is accepted, not completed.** `POST /documents` returns `202` with the row
and an id to watch, because reading a forty-page PDF, chunking it and embedding a few
hundred passages is seconds to minutes inside a request a proxy will give up on. The
client polls the list, which carries the counts that say when to stop.

**Matching writes nothing.** It is a read, on a `POST` only because a query is a body.
`PolicyIdentificationService` is the authority on which policy a claim is under and it
persists candidates an officer can act on; this endpoint says which *wording* answers
the loss and quotes the clauses. Where they agree an officer has corroboration from
two independent methods; where they disagree that is the most useful thing on the
screen, and neither silently wins.

Authorisation splits along the same line. Reading the library and its matches is open
to everyone who reads intake, because the review screen shows an officer which
wordings a notice retrieved. Writing to it is a configuration act — a wording changes
what every future notice is matched against — so it sits with the roles that own
configuration rather than with the officers who work the queue.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.api.deps.auth import require_roles
from app.api.deps.services import PolicyLibraryContext, PolicyLibraryContextDep
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import (
    POLICY_LIBRARY_READ_ROLES,
    POLICY_LIBRARY_WRITE_ROLES,
    PolicyIngestStatus,
)
from app.schemas import policies as api

logger = get_logger(__name__)

router = APIRouter(prefix="/policies", tags=["policies"])

ReadAccess = Annotated[Principal, Depends(require_roles(*POLICY_LIBRARY_READ_ROLES))]
WriteAccess = Annotated[Principal, Depends(require_roles(*POLICY_LIBRARY_WRITE_ROLES))]


def actor_of(principal: Principal) -> str:
    """How a person is named in the record: their name, not their subject."""
    return principal.full_name or principal.username or principal.email or principal.subject


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------


@router.get(
    "/documents",
    response_model=api.PolicyDocumentListResult,
    summary="The policy wordings in the library, and how far each got",
)
async def list_documents(
    principal: ReadAccess,
    context: PolicyLibraryContextDep,
    status_filter: Annotated[list[PolicyIngestStatus] | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> api.PolicyDocumentListResult:
    """One call for the whole board, including the totals the status strip needs.

    The counts are over the whole library rather than the page, deliberately: they are
    what tells the client whether to keep polling, and a count of what happens to be
    on page two would stop it early.
    """
    del principal
    rows, total = await context.documents.list_documents(
        statuses=[value.value for value in status_filter] if status_filter else None,
        search=search,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return api.PolicyDocumentListResult(
        items=[api.to_summary(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        status_counts=await context.documents.status_counts(),
        chunk_total=await context.documents.count_chunks(),
        semantic_search_enabled=context.semantic_enabled,
    )


@router.post(
    "/documents",
    response_model=api.PolicyUploadResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a policy wording for ingestion",
)
async def upload_document(
    # The principal is declared first so authorisation is settled before the session
    # and the file are handled. A caller who may not do this should be refused, not
    # have their 40MB body read to find that out.
    principal: WriteAccess,
    context: PolicyLibraryContextDep,
    file: Annotated[UploadFile, File()],
) -> api.PolicyUploadResult:
    """Accept a PDF, store it, and queue it for ingestion.

    `202`, not `201`: the library entry exists and the passages do not yet. Returning
    `201` would tell the client the resource it asked for — a matchable wording — is
    ready, and it is not.

    Safe to retry. The same bytes are recognised on their checksum and return the
    existing entry rather than a second copy, which matters more here than on the claim
    side: a duplicate wording would be retrieved against, scored, and ranked as a rival
    candidate for the same contract.
    """
    content = await file.read()
    receipt = await context.library.upload(
        filename=file.filename or "policy.pdf",
        content=content,
        declared_content_type=file.content_type,
        actor=actor_of(principal),
    )

    queued = False
    if receipt.needs_ingestion:
        context.library.mark_queued(receipt.document)
        queued = True

    # Committed before the task is enqueued, and that order is the whole point: a
    # worker that picked the id up before this transaction landed would read no row and
    # do nothing, leaving an upload that never ingests and nothing to say why.
    await context.commit()

    if queued:
        queued = _enqueue(receipt.document.id)

    return api.PolicyUploadResult(
        document=api.to_summary(receipt.document),
        duplicate=receipt.duplicate,
        queued=queued,
    )


@router.get(
    "/documents/{document_id}",
    response_model=api.PolicyDocumentSummary,
    summary="One wording's metadata and ingestion status",
)
async def read_document(
    document_id: uuid.UUID,
    principal: ReadAccess,
    context: PolicyLibraryContextDep,
) -> api.PolicyDocumentSummary:
    """The status endpoint. Cheap enough to poll while an upload is in flight."""
    del principal
    return api.to_summary(await context.library.get(document_id))


@router.get(
    "/documents/{document_id}/content",
    summary="Download a policy wording",
)
async def download_document(
    document_id: uuid.UUID,
    principal: ReadAccess,
    context: PolicyLibraryContextDep,
) -> Response:
    """The PDF itself, so a quoted clause can be checked in the document.

    `inline` rather than `attachment`: the point of reaching this endpoint is to read
    page 14 beside the excerpt that quoted it, and a download prompt interrupts exactly
    that. The filename is the sanitised one stored on the row, never the string the
    client sent.
    """
    del principal
    document = await context.library.get(document_id)
    content = await context.library.content(document)
    return Response(
        content=content,
        media_type=document.content_type,
        headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
    )


@router.post(
    "/documents/{document_id}/reingest",
    response_model=api.PolicyIngestResult,
    summary="Read a wording again",
)
async def reingest_document(
    document_id: uuid.UUID,
    principal: WriteAccess,
    context: PolicyLibraryContextDep,
    force: Annotated[bool, Query()] = False,
) -> api.PolicyIngestResult:
    """Re-run ingestion for one wording.

    Inline rather than queued, unlike the upload, and the difference is intentional:
    the text is already on the row, so this is chunking and embedding without the PDF
    parse — and an administrator pressing "retry" on a failed row expects to see the
    outcome rather than to start polling again.

    Without `force` this is idempotent to the point of being free: an unchanged
    document returns `reused: true` after one `SELECT`. `force` is for the case where
    the chunker or the reader has changed and the fingerprint has not caught up.
    """
    del principal
    document = await context.library.get(document_id)
    outcome = await context.ingestion.ingest(document, force=force)
    await context.commit()
    return api.PolicyIngestResult(
        document=api.to_summary(document),
        reused=outcome.reused,
        chunks=outcome.chunks,
        embedded=outcome.embedded,
        linked_policy_id=outcome.linked_policy_id,
        error=outcome.error,
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a wording from the library",
)
async def delete_document(
    document_id: uuid.UUID,
    principal: WriteAccess,
    context: PolicyLibraryContextDep,
) -> Response:
    """Delete a wording, its passages and its vectors.

    The service orders the three stores so a failure leaves the operation retryable
    rather than leaving orphan points nothing can name. See `PolicyLibraryService.remove`.
    """
    document = await context.library.get(document_id)
    logger.info(
        "policy_document_delete_requested",
        document_id=str(document_id),
        actor=actor_of(principal),
    )
    await context.library.remove(document)
    await context.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@router.get(
    "/matches/{reference}",
    response_model=api.PolicyMatchResponse,
    summary="The policy wordings that answer one notification",
)
async def match_case(
    reference: str,
    principal: ReadAccess,
    context: PolicyLibraryContextDep,
) -> api.PolicyMatchResponse:
    """Rank the library against one FNOL case.

    A `GET` because it changes nothing — no candidate rows, no case columns, no
    analysis. That is the safety property rather than a limitation: a wording is a
    document somebody uploaded, and letting a retrieval score bind a contract would
    mean a badly-read declarations page could attach a claim to the wrong policy with
    no book row ever consulted.

    Computed live rather than cached. It is one embedding call per facet plus two
    indexed queries, and a stale answer on a screen an officer is using to bind a
    policy is worse than a fast one.
    """
    del principal
    case = await context.cases.get_by_reference(reference)
    if case is None:
        raise NotFoundError(f"No notification found for {reference}.")

    result = await context.matching.match_case(case)
    return api.to_match_response(result, reference=case.reference)


@router.post(
    "/match",
    response_model=api.PolicyMatchResponse,
    summary="Match the library against a notification or a set of values",
)
async def match(
    payload: api.PolicyMatchRequest,
    principal: ReadAccess,
    context: PolicyLibraryContextDep,
) -> api.PolicyMatchResponse:
    """Match by reference, or by values supplied directly.

    The second form is what makes a freshly loaded library testable: an administrator
    can check that twelve wordings retrieve sensibly by typing an insured name and a
    cause of loss, without first creating a notice. Same engine, same thresholds, same
    explanations — a check that ran through a different path would be checking a
    different thing.
    """
    del principal
    if payload.is_empty:
        raise ValidationError(
            "Give a notification reference, or at least one value to match on.",
            code="policy_match_query_empty",
        )

    if payload.fnol_reference:
        case = await context.cases.get_by_reference(payload.fnol_reference)
        if case is None:
            raise NotFoundError(f"No notification found for {payload.fnol_reference}.")
        result = await context.matching.match_case(case)
        return api.to_match_response(result, reference=case.reference)

    result = await context.matching.match(payload.as_notice())
    return api.to_match_response(result, reference=None)


def _enqueue(document_id: uuid.UUID) -> bool:
    """Ask a worker for this document. Never fatal.

    An unreachable broker must not fail an upload that has already stored its bytes and
    written its row: the document sits at `queued` and the beat sweep picks it up within
    the minute. Returning `False` is what lets the client say "waiting for a worker"
    instead of "ready shortly" — a difference an administrator watching a stalled
    library needs to see.
    """
    try:
        from app.workers.tasks import ingest_policy_document

        ingest_policy_document.delay(str(document_id))
        return True
    except Exception as exc:
        logger.warning(
            "policy_ingest_enqueue_failed",
            document_id=str(document_id),
            error=type(exc).__name__,
        )
        return False


def library_context(context: PolicyLibraryContext) -> PolicyLibraryContext:
    """Re-exported for tests that want to build the context directly."""
    return context
