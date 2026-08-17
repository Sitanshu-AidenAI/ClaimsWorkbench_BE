"""Configurable extraction: the dataset, the run, the values and the evidence.

Two audiences, and the router is split down the middle by them.

`/extraction/schemas` is **configuration**, gated on the administration roles.
Editing a dataset changes what every future notice is read for, which is a
different kind of act from working a queue.

`/fnol/{reference}/extraction…` is **operation**, gated on the ordinary intake
roles. Running a dataset, reading its values and asking where a value came from
are things an officer does all day.

Both live here rather than in `fnol.py` because they are one feature and reading
them together is how the shape stays clear — the values a screen renders and the
field list it renders them under are produced by two routes a hundred lines
apart, and separating them across modules is how those two drift.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps.auth import require_roles
from app.api.deps.services import FNOLContext, FNOLContextDep
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import (
    EXTRACTION_ADMIN_ROLES,
    EXTRACTION_READ_ROLES,
    FNOL_READ_ROLES,
    FNOL_WRITE_ROLES,
    ExtractionSchemaStatus,
    FieldSource,
)
from app.models.extraction import ExtractedValue, ExtractionSchema, ExtractionSchemaField
from app.models.fnol import FNOLCase, FNOLDocument
from app.schemas import extraction as api
from app.services.extraction.locate import rect_from_dict, rect_to_dict
from app.services.extraction.registry import to_dataset
from app.services.extraction.schema import normalise_data_type

logger = get_logger(__name__)

router = APIRouter(tags=["extraction"])

ReadAccess = Annotated[Principal, Depends(require_roles(*FNOL_READ_ROLES))]
WriteAccess = Annotated[Principal, Depends(require_roles(*FNOL_WRITE_ROLES))]
AdminAccess = Annotated[Principal, Depends(require_roles(*EXTRACTION_ADMIN_ROLES))]
#: Reading a dataset *definition* is wider than reading a case: an administrator
#: who owns the dataset must be able to see it, and an officer's browser needs it
#: on every page load to draw the review screen's panels.
SchemaReadAccess = Annotated[Principal, Depends(require_roles(*EXTRACTION_READ_ROLES))]


def actor_of(principal: Principal) -> str:
    return principal.full_name or principal.username or principal.email or principal.subject


# ---------------------------------------------------------------------------
# Dataset configuration
# ---------------------------------------------------------------------------


@router.get(
    "/extraction/schemas",
    response_model=api.SchemaListResult,
    summary="List the configured extraction datasets",
)
async def list_schemas(
    context: FNOLContextDep,
    principal: SchemaReadAccess,
    include_archived: Annotated[bool, Query()] = False,
) -> api.SchemaListResult:
    """Every dataset this desk can read a notice against.

    Readable by anyone who may read intake, not only by an administrator: the
    review screen renders its panels from the dataset, so an officer's browser
    needs it on every page load.
    """
    del principal
    rows = await context.schemas.list(include_archived=include_archived)
    return api.SchemaListResult(items=[api.to_schema(row) for row in rows])


@router.get(
    "/extraction/schemas/{key}",
    response_model=api.SchemaOut,
    summary="One dataset in full",
)
async def get_schema(
    key: str, context: FNOLContextDep, principal: SchemaReadAccess
) -> api.SchemaOut:
    del principal
    return api.to_schema(await _schema_or_404(context, key))


@router.post(
    "/extraction/schemas",
    response_model=api.SchemaOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an extraction dataset",
)
async def create_schema(
    payload: api.SchemaCreate, context: FNOLContextDep, principal: AdminAccess
) -> api.SchemaOut:
    """Define a new set of fields to read out of a notice."""
    if await context.schemas.get_by_key(payload.key) is not None:
        raise ConflictError(f"A dataset with the key {payload.key!r} already exists.")

    schema = ExtractionSchema(
        key=payload.key,
        name=payload.name,
        description=payload.description,
        version=1,
        status=ExtractionSchemaStatus.ACTIVE,
        is_builtin=False,
        is_default=False,
        review_threshold=payload.review_threshold,
        created_by=actor_of(principal),
        updated_by=actor_of(principal),
    )
    schema.fields = [_field_row(field, position) for position, field in enumerate(payload.fields)]
    context.schemas.add(schema)
    await context.schemas.flush()

    if payload.is_default:
        await context.schemas.clear_default(except_id=schema.id)
        schema.is_default = True

    await context.commit()
    logger.info("extraction_schema_created", key=schema.key, fields=len(schema.fields))
    return await _read_back(context, schema.key)


@router.patch(
    "/extraction/schemas/{key}",
    response_model=api.SchemaOut,
    summary="Rename a dataset, or make it the default",
)
async def update_schema(
    key: str, payload: api.SchemaUpdate, context: FNOLContextDep, principal: AdminAccess
) -> api.SchemaOut:
    schema = await _schema_or_404(context, key)

    if payload.name is not None:
        schema.name = payload.name
    if payload.description is not None:
        schema.description = payload.description
    if payload.review_threshold is not None:
        schema.review_threshold = payload.review_threshold
    if payload.status is not None:
        if payload.status != ExtractionSchemaStatus.ACTIVE and schema.is_default:
            raise ValidationError(
                "The default dataset cannot be archived. Make another dataset the default first."
            )
        schema.status = payload.status

    if payload.is_default is True:
        if schema.status != ExtractionSchemaStatus.ACTIVE:
            raise ValidationError("Only an active dataset can be the default.")
        # Cleared first: the single-default rule is a partial unique index, so two
        # defaults is an integrity error rather than a last-write-wins.
        await context.schemas.clear_default(except_id=schema.id)
        schema.is_default = True
    elif payload.is_default is False and schema.is_default:
        raise ValidationError(
            "Make another dataset the default instead of unsetting this one — a "
            "desk with no default dataset reads nothing out of a notice."
        )

    schema.updated_by = actor_of(principal)
    await context.commit()
    return await _read_back(context, schema.key)


@router.put(
    "/extraction/schemas/{key}/fields",
    response_model=api.SchemaOut,
    summary="Replace a dataset's fields",
)
async def replace_fields(
    key: str,
    payload: api.SchemaFieldsReplace,
    context: FNOLContextDep,
    principal: AdminAccess,
) -> api.SchemaOut:
    """Save the whole field list, in the order it should be asked in.

    The whole list rather than a field at a time, because that is how the list is
    edited: reordered, renamed and pruned in one save. Reconciling that field by
    field would need a client-supplied identity for a row the client has only
    just invented.

    Saving bumps the dataset's version, which moves every open notice's
    extraction fingerprint and causes them to be re-read on their next run. That
    is the intended cost of changing the questions: values extracted against a
    question nobody asks any more are values nobody can defend.
    """
    schema = await _schema_or_404(context, key)

    rows = [_field_row(field, position) for position, field in enumerate(payload.fields)]
    await context.schemas.replace_fields(schema, rows)
    schema.version += 1
    schema.updated_by = actor_of(principal)

    await context.commit()
    logger.info(
        "extraction_schema_fields_replaced",
        key=schema.key,
        version=schema.version,
        fields=len(rows),
    )
    return await _read_back(context, schema.key)


@router.delete(
    "/extraction/schemas/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a dataset",
)
async def delete_schema(key: str, context: FNOLContextDep, principal: AdminAccess) -> None:
    """Remove a dataset and everything extracted against it.

    Refused for a built-in dataset and for the default one. A built-in is what
    the product falls back to; the default is what the pipeline runs. Deleting
    either leaves a desk whose notices are read for nothing, and the recovery
    would be a redeploy.
    """
    del principal
    schema = await _schema_or_404(context, key)
    if schema.is_builtin:
        raise ValidationError(
            "This dataset ships with the product and cannot be deleted. Its fields "
            "can be edited, and it can be archived once another is the default."
        )
    if schema.is_default:
        raise ValidationError("Make another dataset the default before deleting this one.")

    await context.schemas.delete(schema)
    await context.commit()


# ---------------------------------------------------------------------------
# Running a dataset against a notice
# ---------------------------------------------------------------------------


@router.get(
    "/fnol/{reference}/extraction",
    response_model=api.ExtractionResult,
    summary="The dataset, the last run and the values",
)
async def get_extraction(
    reference: str,
    context: FNOLContextDep,
    principal: ReadAccess,
    schema_key: Annotated[str | None, Query()] = None,
) -> api.ExtractionResult:
    """Everything the review screen needs to draw the extracted record.

    One call, carrying the dataset as well as the values: a client rendering a
    configurable field set needs the order, the grouping and the labels, and
    fetching those separately is how a panel of values ends up under the wrong
    headings for the half-second between two responses.
    """
    del principal
    case = await context.fnol.get(reference)
    schema = await _target_schema(context, schema_key)

    run = await context.runs.latest_run(case.id, schema.id)
    values = await context.runs.list_values(case.id, schema.id)
    filenames = await _filenames(context, case)
    required = {field.key for field in schema.fields if field.required}

    return api.ExtractionResult(
        reference=case.reference,
        dataset=api.to_schema(schema),
        run=api.to_run(run) if run is not None else None,
        values=[
            api.to_value(
                value,
                filename=filenames.get(value.source_document_id),
                required=value.field_key in required,
            )
            for value in values
        ],
        note=None
        if run is not None
        else "This notification has not been read against this dataset yet.",
    )


@router.post(
    "/fnol/{reference}/extraction",
    response_model=api.ExtractionResult,
    summary="Read the notification against a dataset",
)
async def run_extraction(
    reference: str,
    payload: api.ExtractionRequest,
    context: FNOLContextDep,
    principal: WriteAccess,
) -> api.ExtractionResult:
    """Run the dataset now, and re-run the pipeline behind it.

    Runs inline rather than enqueueing: a caller who pressed this wants the
    answer, and the fingerprint guard means an unchanged notice costs one query.
    A notice whose documents have not been indexed yet is indexed first — reading
    a dataset out of a case with no passages would return nothing and look like a
    model failure.
    """
    case = await context.fnol.get(reference)
    schema = await _target_schema(context, payload.schema_key)

    if payload.run_pipeline:
        # The pipeline owns the body document, the index and the write-back, and
        # running the engine outside it would produce values the claim record
        # never saw. `force` propagates: an officer asking for a re-read means it.
        await context.pipeline.run(case, force=payload.force)
    else:
        await context.body.ensure(case)
        documents = list(await context.cases.list_documents(case.id))
        index = await context.index.index_case(documents, force=False)
        outcome = await context.engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=index.signature,
            force=payload.force,
            triggered_by=actor_of(principal),
        )
        if outcome.skipped_reason:
            logger.info(
                "extraction_run_skipped",
                reference=case.reference,
                reason=outcome.skipped_reason,
            )

    await context.commit()
    return await get_extraction(reference, context, principal, schema_key=schema.key)


@router.patch(
    "/fnol/{reference}/extraction/values/{field_key}",
    response_model=api.ExtractedValueOut,
    summary="Correct an extracted value",
)
async def update_value(
    reference: str,
    field_key: str,
    payload: api.ValueUpdate,
    context: FNOLContextDep,
    principal: WriteAccess,
    schema_key: Annotated[str | None, Query()] = None,
) -> api.ExtractedValueOut:
    """Record an officer's correction, permanently.

    What the model read is kept in `original_value` rather than overwritten, and
    `human_modified` stops every later run from replacing the correction. That
    flag is checked in three places — here, in the engine, and in the write-back
    adapter — because losing an officer's correction is the single most damaging
    thing this feature could do.
    """
    case = await context.fnol.get(reference)
    schema = await _target_schema(context, schema_key)

    value = await context.runs.get_value(case.id, schema.id, field_key)
    if value is None:
        raise NotFoundError(
            f"No field {field_key!r} has been recorded on this notification for "
            f"the {schema.key} dataset."
        )

    spec = to_dataset(schema).field(field_key)
    cleaned = (payload.value or "").strip() or None

    if not value.human_modified:
        value.original_value = value.value_text
    value.value_text = cleaned
    value.source = FieldSource.HUMAN
    value.human_modified = True
    value.modified_by = actor_of(principal)
    value.modified_at = datetime.now(UTC)
    value.override_reason = payload.reason
    value.needs_review = False
    # A person's answer has no model confidence and no passage behind it. Keeping
    # the old citation would point at the sentence they disagreed with.
    value.confidence = None
    value.source_chunk_id = None
    value.source_document_id = None
    value.page_number = None
    value.section_label = None
    value.quote = None
    value.char_start = None
    value.char_end = None
    value.rects = None
    value.highlight_note = None

    if spec is not None:
        coerced, error = spec.coerce(cleaned)
        value.value_json = coerced if cleaned is not None else None
        value.validation_error = error
    else:
        value.value_json = None
        value.validation_error = None

    # Mirrored onto the claim record in the same transaction, so the review
    # screen and the pipeline cannot disagree about what the value is.
    await context.pipeline.write_back(case, [value])
    await context.commit()

    logger.info(
        "extraction_value_corrected",
        reference=case.reference,
        schema=schema.key,
        field=field_key,
    )
    filenames = await _filenames(context, case)
    return api.to_value(
        value,
        filename=filenames.get(value.source_document_id),
        required=bool(spec and spec.required),
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@router.get(
    "/fnol/{reference}/extraction/values/{field_key}/evidence",
    response_model=api.EvidenceOut,
    summary="Show where a value came from",
)
async def value_evidence(
    reference: str,
    field_key: str,
    context: FNOLContextDep,
    principal: ReadAccess,
    schema_key: Annotated[str | None, Query()] = None,
    refresh: Annotated[bool, Query()] = False,
) -> api.EvidenceOut:
    """The document, page, text and rectangles behind one extracted value.

    Answers at whatever precision the file allows and always says which it
    reached, so a viewer showing no highlight can say *why* rather than showing a
    page with nothing marked on it.

    Rectangles are cached on the value row after the first request. Resolving
    them means downloading the file and parsing a page's word geometry, and a
    reviewer stepping between two fields on one document should not pay that
    twice. `refresh=true` recomputes them.
    """
    del principal
    case = await context.fnol.get(reference)
    schema = await _target_schema(context, schema_key)

    value = await context.runs.get_value(case.id, schema.id, field_key)
    if value is None:
        raise NotFoundError(f"No field {field_key!r} has been recorded on this notification.")

    document = (
        await context.cases.get_document(value.source_document_id)
        if value.source_document_id is not None
        else None
    )
    if document is None:
        return _evidence_without_document(value)

    chunk = (
        await context.chunks.get(value.source_chunk_id)
        if value.source_chunk_id is not None
        else None
    )

    cached = value.rects
    if cached is not None and not refresh:
        return _evidence(
            value,
            document,
            rects=[rect_from_dict(rect) for rect in cached],
            text=value.quote,
            char_start=value.char_start,
            char_end=value.char_end,
            page_number=value.page_number,
            section_label=value.section_label,
            strategy="chunk-grounded" if chunk is not None else "document-search",
            note=value.highlight_note,
        )

    location = await context.locator.resolve_evidence(
        document, chunk, quote=value.quote, value=value.value_text
    )

    if context.extraction_config.cache_highlight_rects:
        value.rects = [rect_to_dict(rect) for rect in location.rects]
        value.highlight_note = location.note
        if location.page_number is not None:
            value.page_number = location.page_number
        await context.commit()

    return _evidence(
        value,
        document,
        rects=location.rects,
        text=location.text,
        char_start=location.char_start,
        char_end=location.char_end,
        page_number=location.page_number,
        section_label=location.section_label or value.section_label,
        strategy=location.strategy,
        note=location.note,
    )


@router.post(
    "/fnol/{reference}/documents/{document_id}/locate",
    response_model=api.LocateResult,
    summary="Find every place a piece of text appears in a document",
)
async def locate_text(
    reference: str,
    document_id: uuid.UUID,
    payload: api.LocateRequest,
    context: FNOLContextDep,
    principal: ReadAccess,
) -> api.LocateResult:
    """Every occurrence of `text`, with its page and its rectangles.

    What a reviewer needs *after* the evidence endpoint has answered: the cited
    occurrence is one of several, and stepping between them is how a person
    checks that the one the model read is the one that matters. It is also the
    only answer available for a value with no citation — one an officer typed, or
    one whose passage was replaced by a re-index.
    """
    del principal
    case = await context.fnol.get(reference)
    document = await context.cases.get_document(document_id)
    if document is None or document.fnol_case_id != case.id:
        raise NotFoundError("That document is not attached to this notification.")

    found = await context.locator.locate_in_document(document, payload.text, limit=payload.limit)
    return api.LocateResult(
        document_id=document.id,
        filename=document.filename,
        total=len(found),
        items=[api.to_occurrence(occurrence) for occurrence in found],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _schema_or_404(context: FNOLContext, key: str) -> ExtractionSchema:
    schema = await context.schemas.get_by_key(key)
    if schema is None:
        raise NotFoundError(f"No extraction dataset with the key {key!r}.")
    return schema


async def _read_back(context: FNOLContext, key: str) -> api.SchemaOut:
    """Re-read a dataset after writing it, and map that.

    Mapping the in-memory object would be one query cheaper and is wrong: the
    field list is replaced by a bulk `DELETE` plus a set of inserts, which leaves
    the relationship stale, and a stale collection read outside the async context
    raises `MissingGreenlet` rather than returning old data. Re-reading is also
    the honest response — it is what the next `GET` will return.
    """
    return api.to_schema(await _schema_or_404(context, key))


async def _target_schema(context: FNOLContext, key: str | None) -> ExtractionSchema:
    """The dataset a request means: the one it named, or the default."""
    if key:
        return await _schema_or_404(context, key)
    schema = await context.schemas.get_default()
    if schema is None:
        raise NotFoundError(
            "No default extraction dataset is configured. Create one, or mark an "
            "existing dataset as the default."
        )
    return schema


async def _filenames(context: FNOLContext, case: FNOLCase) -> dict[uuid.UUID | None, str]:
    documents: list[FNOLDocument] = list(await context.cases.list_documents(case.id))
    return {document.id: document.filename for document in documents}


def _evidence_without_document(value: ExtractedValue) -> api.EvidenceOut:
    """The honest answer for a value with nothing to point at.

    Not an error: a value an officer typed, or one whose document was deleted,
    has no source and saying so is the correct response.
    """
    return api.EvidenceOut(
        field_key=value.field_key,
        label=value.label,
        value=value.value_text,
        confidence=float(value.confidence) if value.confidence is not None else None,
        document_id=None,
        filename=None,
        content_type=None,
        document_source=None,
        chunk_id=None,
        page_number=None,
        page_count=None,
        section_label=None,
        text=value.quote,
        char_start=None,
        char_end=None,
        rects=[],
        strategy="none",
        note=(
            "This value was entered by an officer."
            if value.human_modified
            else "This value has no document passage recorded against it."
        ),
    )


def _evidence(
    value: ExtractedValue,
    document: FNOLDocument,
    *,
    rects: list[Any],
    text: str | None,
    char_start: int | None,
    char_end: int | None,
    page_number: int | None,
    section_label: str | None,
    strategy: str,
    note: str | None,
) -> api.EvidenceOut:
    return api.EvidenceOut(
        field_key=value.field_key,
        label=value.label,
        value=value.value_text,
        confidence=float(value.confidence) if value.confidence is not None else None,
        document_id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        document_source=document.source,
        chunk_id=value.source_chunk_id,
        page_number=page_number,
        page_count=document.page_count,
        section_label=section_label,
        text=text,
        char_start=char_start,
        char_end=char_end,
        rects=[api.to_rect(rect) for rect in rects],
        strategy=strategy,
        note=note,
    )


def _field_row(field: api.SchemaFieldIn, position: int) -> ExtractionSchemaField:
    return ExtractionSchemaField(
        key=field.key,
        label=field.label,
        description=field.description,
        data_type=normalise_data_type(field.data_type),
        group_label=field.group_label,
        required=field.required,
        enabled=field.enabled,
        position=position,
        aliases=field.aliases or None,
        extraction_hint=field.extraction_hint,
    )
