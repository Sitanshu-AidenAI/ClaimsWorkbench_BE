"""Request and response shapes for configurable extraction.

Built by explicit mappers at the foot of the module, the same way
`app.schemas.fnol` is and for the same reason: adding a column to a table must
not be able to publish it by accident.

The highlight payload is the one worth reading before writing a viewer against
it. It answers at four levels of precision and always says which it reached, so
the client never has to guess whether an empty `rects` means "no geometry here"
or "something went wrong":

    document + page   →  always, when there is a citation
    exact text        →  always, with offsets into the document's stored text
    rectangles        →  PDFs whose words could be located
    page dimensions   →  on every rectangle, so a viewer can scale without asking
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator

from app.schemas.common import SchemaBase
from app.services.extraction.schema import DATA_TYPES

# ---------------------------------------------------------------------------
# Dataset configuration
# ---------------------------------------------------------------------------

#: A field key is the dotted address a value is stored and served under. Bounded
#: to what a URL path segment and a database column can carry, and restricted so
#: that a key cannot contain a character that would need escaping in one of the
#: four places it appears.
FIELD_KEY_PATTERN = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
SCHEMA_KEY_PATTERN = r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$"


class SchemaFieldIn(SchemaBase):
    """One field, as the configuration screen submits it."""

    key: Annotated[str, Field(pattern=FIELD_KEY_PATTERN, max_length=96)]
    label: Annotated[str, Field(min_length=1, max_length=128)]
    #: The retrieval query. Long is good here — it is prose in a document's
    #: vocabulary, and it is the single biggest lever on whether a field is found.
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    data_type: str = "string"
    group_label: Annotated[str, Field(min_length=1, max_length=64)] = "Fields"
    required: bool = False
    enabled: bool = True
    aliases: Annotated[list[str], Field(max_length=40, default_factory=list)]
    extraction_hint: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("data_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        cleaned = (value or "string").strip().lower()
        if cleaned not in DATA_TYPES:
            raise ValueError(
                f"Unknown data type {value!r}. One of: {', '.join(sorted(DATA_TYPES))}."
            )
        return cleaned

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for alias in value:
            trimmed = alias.strip()[:96]
            if trimmed and trimmed not in seen:
                seen.append(trimmed)
        return seen


class SchemaFieldOut(SchemaBase):
    id: uuid.UUID
    key: str
    label: str
    description: str
    data_type: str
    group_label: str
    required: bool
    enabled: bool
    position: int
    aliases: list[str]
    extraction_hint: str | None


class SchemaCreate(SchemaBase):
    key: Annotated[str, Field(pattern=SCHEMA_KEY_PATTERN, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str | None, Field(max_length=4000)] = None
    review_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.6
    is_default: bool = False
    fields: Annotated[list[SchemaFieldIn], Field(max_length=400, default_factory=list)]


class SchemaUpdate(SchemaBase):
    """Everything editable about a dataset except its fields.

    `None` means "leave alone" throughout. A dataset's `key` is deliberately not
    editable: a run points at it and a URL is written against it.
    """

    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    description: Annotated[str | None, Field(max_length=4000)] = None
    review_threshold: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    status: str | None = None
    is_default: bool | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned not in {"draft", "active", "archived"}:
            raise ValueError("Status must be draft, active or archived.")
        return cleaned


class SchemaFieldsReplace(SchemaBase):
    """The whole field list, in the order it should be asked in."""

    fields: Annotated[list[SchemaFieldIn], Field(max_length=400)]

    @field_validator("fields")
    @classmethod
    def _unique_keys(cls, value: list[SchemaFieldIn]) -> list[SchemaFieldIn]:
        seen: set[str] = set()
        for item in value:
            if item.key in seen:
                raise ValueError(f"Duplicate field key: {item.key}.")
            seen.add(item.key)
        return value


class SchemaOut(SchemaBase):
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    version: int
    status: str
    is_builtin: bool
    is_default: bool
    review_threshold: float
    field_count: int
    #: The panels this dataset draws, in the order its fields declare them.
    groups: list[str]
    fields: list[SchemaFieldOut]
    updated_at: datetime


class SchemaListResult(SchemaBase):
    items: list[SchemaOut]


# ---------------------------------------------------------------------------
# Running a dataset
# ---------------------------------------------------------------------------


class ExtractionRequest(SchemaBase):
    """Run a dataset against a notice."""

    #: The dataset to run. Omitted means the default one.
    schema_key: Annotated[str | None, Field(pattern=SCHEMA_KEY_PATTERN, max_length=64)] = None
    #: Re-read even when nothing the extraction depends on has moved. What an
    #: officer presses after correcting a document, or after editing the dataset.
    force: bool = False
    #: Re-run the rest of the pipeline afterwards — classification, policy
    #: matching, completeness, exceptions. On by default because new values that
    #: do not move the exception list are values nobody acts on.
    run_pipeline: bool = True


class HighlightRect(SchemaBase):
    """One rectangle to draw, in PDF points from the top-left of the page.

    `page_width` and `page_height` travel with every rectangle so a viewer can
    scale to whatever size it renders at without a second request.
    """

    page_number: int
    x0: float
    top: float
    x1: float
    bottom: float
    page_width: float
    page_height: float


class ExtractedValueOut(SchemaBase):
    """One field's value and everything needed to show where it came from."""

    field_key: str
    label: str
    group_label: str
    data_type: str

    value: str | None
    #: The coerced form, for a type that has one — an ISO date, minor units for
    #: money, a parsed JSON array. Null for a plain string, whose coerced form is
    #: the value itself.
    typed_value: Any = None
    confidence: float | None
    needs_review: bool
    #: Why the value could not be read as its declared type, when it could not.
    #: The value is still shown: real text a document really states is worth more
    #: than a null that is technically well-typed.
    validation_error: str | None
    #: How `typed_value` was arrived at, when it was inferred rather than copied —
    #: "“overnight on Friday” is read as 1 May 2026 22:00, relative to the
    #: notification of 5 May 2026". Shown beside the value so an officer sees the
    #: broker's words and this service's reading of them at the same time, and can
    #: correct the one if they disagree with the other.
    inference_note: str | None = None
    required: bool
    source: str

    human_modified: bool
    original_value: str | None
    modified_by: str | None
    modified_at: datetime | None
    override_reason: str | None

    #: Non-null means the evidence endpoint has something to show for this field.
    source_chunk_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    source_document_filename: str | None
    page_number: int | None
    section_label: str | None
    quote: str | None


class ExtractionRunOut(SchemaBase):
    id: uuid.UUID
    schema_key: str
    schema_version: int
    status: str
    fields_total: int
    fields_extracted: int
    fields_needing_review: int
    fields_failed: int
    retrieval_strategy: str | None
    degraded: bool
    chunks_available: int
    provider: str | None
    model: str | None
    llm_calls: int
    latency_ms: int
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None


class ExtractionResult(SchemaBase):
    """A dataset, its run and its values — everything the review screen needs.

    The schema travels with the values deliberately. A client rendering a
    configurable dataset needs the field order, the grouping and the labels, and
    fetching them separately would let the two arrive out of step and draw a
    panel of values under the wrong headings.
    """

    reference: str
    dataset: SchemaOut
    run: ExtractionRunOut | None
    values: list[ExtractedValueOut]
    #: Set when no run has happened yet or the dataset could not be run.
    note: str | None = None


class ValueUpdate(SchemaBase):
    """An officer correcting one extracted value."""

    value: Annotated[str | None, Field(max_length=8000)]
    reason: Annotated[str | None, Field(max_length=1000)] = None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class EvidenceOut(SchemaBase):
    """Where one value can be seen in its document."""

    field_key: str
    label: str
    value: str | None
    confidence: float | None

    document_id: uuid.UUID | None
    filename: str | None
    content_type: str | None
    #: `email_body`, `email_attachment` or `upload`. The viewer picks its renderer
    #: from this and `content_type` together.
    document_source: str | None

    chunk_id: uuid.UUID | None
    page_number: int | None
    page_count: int | None
    section_label: str | None

    #: The text to mark. The model's quote when it could be located inside the
    #: cited passage, otherwise the passage itself, capped.
    text: str | None
    #: Offsets into the document's stored text, for a viewer that renders text
    #: rather than pages.
    char_start: int | None
    char_end: int | None

    rects: list[HighlightRect]
    #: `chunk-grounded` | `document-search` | `text-only` | `none`.
    strategy: str
    note: str | None


class LocateRequest(SchemaBase):
    """Find every place a piece of text appears in one document."""

    text: Annotated[str, Field(min_length=2, max_length=500)]
    limit: Annotated[int, Field(ge=1, le=50)] = 20


class OccurrenceOut(SchemaBase):
    page_number: int | None
    char_start: int
    char_end: int
    snippet: str
    rects: list[HighlightRect]


class LocateResult(SchemaBase):
    document_id: uuid.UUID
    filename: str
    total: int
    items: list[OccurrenceOut]


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def to_schema_field(row: Any) -> SchemaFieldOut:
    return SchemaFieldOut(
        id=row.id,
        key=row.key,
        label=row.label,
        description=row.description,
        data_type=row.data_type,
        group_label=row.group_label,
        required=row.required,
        enabled=row.enabled,
        position=row.position,
        aliases=list(row.aliases or []),
        extraction_hint=row.extraction_hint,
    )


def to_schema(row: Any) -> SchemaOut:
    fields = sorted(row.fields, key=lambda field: (field.position, field.key))
    groups: list[str] = []
    for field in fields:
        if field.enabled and field.group_label not in groups:
            groups.append(field.group_label)

    return SchemaOut(
        id=row.id,
        key=row.key,
        name=row.name,
        description=row.description,
        version=row.version,
        status=row.status,
        is_builtin=row.is_builtin,
        is_default=row.is_default,
        review_threshold=float(row.review_threshold),
        field_count=sum(1 for field in fields if field.enabled),
        groups=groups,
        fields=[to_schema_field(field) for field in fields],
        updated_at=row.updated_at,
    )


def to_run(row: Any) -> ExtractionRunOut:
    return ExtractionRunOut(
        id=row.id,
        schema_key=row.schema_key,
        schema_version=row.schema_version,
        status=row.status,
        fields_total=row.fields_total,
        fields_extracted=row.fields_extracted,
        fields_needing_review=row.fields_needing_review,
        fields_failed=row.fields_failed,
        retrieval_strategy=row.retrieval_strategy,
        degraded=row.degraded,
        chunks_available=row.chunks_available,
        provider=row.provider,
        model=row.model,
        llm_calls=row.llm_calls,
        latency_ms=row.latency_ms,
        error=row.error,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def to_value(row: Any, *, filename: str | None = None, required: bool = False) -> ExtractedValueOut:
    return ExtractedValueOut(
        field_key=row.field_key,
        label=row.label,
        group_label=row.group_label,
        data_type=row.data_type,
        value=row.value_text,
        typed_value=row.value_json,
        confidence=float(row.confidence) if row.confidence is not None else None,
        needs_review=row.needs_review,
        validation_error=row.validation_error,
        inference_note=row.inference_note,
        required=required,
        source=row.source,
        human_modified=row.human_modified,
        original_value=row.original_value,
        modified_by=row.modified_by,
        modified_at=row.modified_at,
        override_reason=row.override_reason,
        source_chunk_id=row.source_chunk_id,
        source_document_id=row.source_document_id,
        source_document_filename=filename,
        page_number=row.page_number,
        section_label=row.section_label,
        quote=row.quote,
    )


def to_rect(rect: Any) -> HighlightRect:
    return HighlightRect(
        page_number=rect.page_number,
        x0=rect.x0,
        top=rect.top,
        x1=rect.x1,
        bottom=rect.bottom,
        page_width=rect.page_width,
        page_height=rect.page_height,
    )


def to_occurrence(occurrence: Any) -> OccurrenceOut:
    return OccurrenceOut(
        page_number=occurrence.page_number,
        char_start=occurrence.char_start,
        char_end=occurrence.char_end,
        snippet=occurrence.snippet,
        rects=[to_rect(rect) for rect in occurrence.rects],
    )


__all__ = [
    "FIELD_KEY_PATTERN",
    "SCHEMA_KEY_PATTERN",
    "EvidenceOut",
    "ExtractedValueOut",
    "ExtractionRequest",
    "ExtractionResult",
    "ExtractionRunOut",
    "HighlightRect",
    "LocateRequest",
    "LocateResult",
    "OccurrenceOut",
    "SchemaCreate",
    "SchemaFieldIn",
    "SchemaFieldOut",
    "SchemaFieldsReplace",
    "SchemaListResult",
    "SchemaOut",
    "SchemaUpdate",
    "ValueUpdate",
    "to_occurrence",
    "to_rect",
    "to_run",
    "to_schema",
    "to_schema_field",
    "to_value",
]
