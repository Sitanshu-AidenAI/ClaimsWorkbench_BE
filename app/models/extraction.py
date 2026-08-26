"""Configurable extraction: the dataset, its fields, a run, and the values.

The FNOL module reads a notice into a fixed set of columns on `fnol_cases`. That
is the *claim record*, and it is right that it is fixed — a claim has a policy
number whatever a carrier calls it. This module is the other half: **what to look
for is configuration, not code.**

Four tables, and the split is the lifecycle:

* `extraction_schemas` — a named dataset. "FNOL notice" is one. A carrier writing
  a different book adds another without a deployment.
* `extraction_schema_fields` — the fields in it. A field's `description` is the
  interesting column: it is prose written in the vocabulary a *document* uses,
  and it is used verbatim as the retrieval query. "The policy or certificate
  number the risk is written under, usually near the top of a schedule or
  slip" retrieves an address block that `policy_number` does not.
* `extraction_runs` — one execution of one schema against one notice. Carries the
  fingerprint that makes a re-run free, and the per-run counts that make a
  partial failure visible instead of silent.
* `extracted_values` — one value per field, with the passage it was read from.
* `extracted_value_citations` — every document that states that value. One row is
  one place a reviewer can be taken to see it, and a value routinely has several:
  the broker's email, the completed notice form and the engineer's report can all
  print the same policy number, and "which of the three did we read" and "do the
  three agree" are different questions an officer needs both answers to.

`extracted_values` is deliberately not `fnol_extracted_fields`. That table is the
review screen's contract and is keyed by the case's own field paths; this one is
keyed by a *schema's* field keys and can hold a dataset that has nothing to do
with the claim record. They are kept in step for the FNOL dataset by
`app.services.fnol.adapter`, which is the only place that knows both.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ExtractionRunStatus, ExtractionSchemaStatus, FieldSource


class ExtractionSchema(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One configurable dataset — the set of fields to read out of an intake."""

    __tablename__ = "extraction_schemas"

    #: The stable public name, used in URLs and by the pipeline to pick a dataset.
    #: Lower-case, dotted or dashed; never renamed, because a run points at it.
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)

    #: Bumped whenever the field set changes. Folded into the run fingerprint, so
    #: editing a field's description re-reads every open notice rather than
    #: leaving values that were extracted against a question nobody asks now.
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default=ExtractionSchemaStatus.ACTIVE)

    #: True for a dataset shipped with the product. Its fields are editable — that
    #: is the whole point — but it cannot be deleted, because the pipeline falls
    #: back to it and a desk with no dataset extracts nothing.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Exactly one schema is the default the FNOL pipeline runs. Enforced by a
    #: partial unique index rather than by application code, so two concurrent
    #: edits cannot both win.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Below this, a value is flagged for a human rather than trusted. 0 to 1, to
    #: match `confidence` everywhere else in this codebase.
    review_threshold: Mapped[float] = mapped_column(Numeric(4, 3, asdecimal=False), default=0.600)

    created_by: Mapped[str | None] = mapped_column(String(255))
    updated_by: Mapped[str | None] = mapped_column(String(255))

    fields: Mapped[list[ExtractionSchemaField]] = relationship(
        back_populates="schema",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExtractionSchemaField.position",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("key", name="uq_extraction_schemas_key"),
        # Exactly one default, enforced by Postgres. Partial, so the common case
        # — every other row — is not constrained at all.
        Index(
            "uq_extraction_schemas_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "review_threshold >= 0 AND review_threshold <= 1",
            name="review_threshold_bounded",
        ),
    )


class ExtractionSchemaField(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One field to extract, and how to find it."""

    __tablename__ = "extraction_schema_fields"

    schema_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extraction_schemas.id", ondelete="CASCADE"), index=True
    )

    #: The dotted address the value is stored and served under. For the built-in
    #: FNOL dataset these are exactly the paths the review screen already uses
    #: (`policy.policy_number`), which is what lets the adapter mirror a value
    #: onto the claim record without a translation table.
    key: Mapped[str] = mapped_column(String(96))
    label: Mapped[str] = mapped_column(String(128))

    #: **The retrieval query.** Prose in the vocabulary a document uses, not the
    #: vocabulary the schema uses. This is the single highest-leverage column in
    #: the module: the difference between finding a value and not usually lives
    #: here rather than in the model or the chunker.
    description: Mapped[str] = mapped_column(Text)

    #: `string | text | integer | number | money | date | datetime | boolean | json`.
    #: Coerced by `app.services.extraction.schema`; a value that will not coerce is
    #: kept as text and flagged, never silently dropped.
    data_type: Mapped[str] = mapped_column(String(16), default="string")

    #: The panel this field is drawn in. Free text so a new dataset invents its
    #: own grouping without a migration.
    group_label: Mapped[str] = mapped_column(String(64), default="Fields")

    required: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    #: Other names a document might use for this field. Appended to the retrieval
    #: query and shown to the model, so "sum insured" finds "total insured value".
    aliases: Mapped[list[str] | None] = mapped_column(JSONB)

    #: A short instruction for the model about this field only — format, units, a
    #: disambiguation. Kept apart from `description` because one is a *query* and
    #: the other is a *rule*, and mixing them makes retrieval worse.
    extraction_hint: Mapped[str | None] = mapped_column(Text)

    #: The description's embedding, cached because it is static per field version.
    #: Without this, a 40-field dataset costs 40 embedding round-trips on every
    #: run of every notice.
    query_embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    query_embedding_model: Mapped[str | None] = mapped_column(String(96))
    #: sha256 of the text that was embedded. A description edit invalidates the
    #: cache without anyone having to remember to clear it.
    query_embedding_hash: Mapped[str | None] = mapped_column(String(64))

    schema: Mapped[ExtractionSchema] = relationship(back_populates="fields")

    __table_args__ = (
        UniqueConstraint("schema_id", "key", name="uq_extraction_schema_field_key"),
        CheckConstraint("position >= 0", name="field_position_non_negative"),
    )


class ExtractionRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One execution of one schema against one notice.

    Exists so that "what happened" is a row rather than a log line. It is what a
    retry reads to decide whether there is anything to do, what the review screen
    reads to say "6 of 34 fields need checking", and what a future agent step
    would resume from.
    """

    __tablename__ = "extraction_runs"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extraction_schemas.id", ondelete="CASCADE"), index=True
    )
    schema_key: Mapped[str] = mapped_column(String(64))
    #: Denormalised so a run still reports which version of the dataset produced
    #: it after the schema has moved on.
    schema_version: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[str] = mapped_column(String(16), default=ExtractionRunStatus.QUEUED)

    #: `sha256(notice text | document signatures | schema version | field set)`.
    #: Unchanged and `completed` means the whole run is skipped — no retrieval, no
    #: model call, no rows written.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)

    fields_total: Mapped[int] = mapped_column(Integer, default=0)
    fields_extracted: Mapped[int] = mapped_column(Integer, default=0)
    fields_needing_review: Mapped[int] = mapped_column(Integer, default=0)
    fields_failed: Mapped[int] = mapped_column(Integer, default=0)

    #: How retrieval answered — `hybrid-rrf | semantic | keyword | whole-corpus |
    #: none`. Surfaced rather than inferred, so a thin result set explains itself.
    retrieval_strategy: Mapped[str | None] = mapped_column(String(32))
    #: A backend that should have answered did not. Not the same as one that was
    #: never configured.
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    chunks_available: Mapped[int] = mapped_column(Integer, default=0)

    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(96))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_by: Mapped[str | None] = mapped_column(String(255))

    values: Mapped[list[ExtractedValue]] = relationship(
        back_populates="run",
        # The run is a record of what happened; the values outlive it under
        # `SET NULL` so superseding a run keeps the values it produced until the
        # next one replaces them by key.
        passive_deletes=True,
        lazy="raise",
    )

    __table_args__ = (
        Index("ix_extraction_runs_case_schema", "fnol_case_id", "schema_id"),
        CheckConstraint("fields_total >= 0", name="fields_total_non_negative"),
    )


class ExtractedValue(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One field's value, with the passage it was read from.

    One row per `(case, schema, field_key)`, updated in place. That is what makes
    a re-run idempotent from the review screen's point of view: a value does not
    move, it changes, and a human correction on it survives the next run.

    The document, passage, page, offsets and quote of the *primary* citation — the
    passage the model says it read the value from — are denormalised onto this row
    so the review screen renders a value and its source without a join.
    `citations` carries that same passage as its first row plus every other
    document found to state the same value, and it is what the source stepper in
    the viewer walks. Rectangles live there and nowhere else: they need the file's
    bytes and a page's word geometry, so they are resolved on demand, per
    citation, and cached on the citation row.
    """

    __tablename__ = "extracted_values"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extraction_schemas.id", ondelete="CASCADE"), index=True
    )
    #: The run that last wrote this value. `SET NULL` rather than `CASCADE`:
    #: pruning old runs must not delete the values a case is currently showing.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extraction_runs.id", ondelete="SET NULL")
    )

    field_key: Mapped[str] = mapped_column(String(96))
    #: Denormalised from the field row so a value renders without a join, and
    #: still renders after the field has been renamed or removed from the schema.
    label: Mapped[str] = mapped_column(String(128))
    group_label: Mapped[str] = mapped_column(String(64), default="Fields")
    data_type: Mapped[str] = mapped_column(String(16), default="string")

    #: Always populated when there is a value — the text as the document states
    #: it, before coercion. `value_json` carries the coerced form when the type is
    #: not a string, so a bad date is visible rather than lost.
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[object | None] = mapped_column(JSONB)

    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Set when the value could not be coerced to its declared type, or a
    #: validation rule rejected it. The value is still stored.
    validation_error: Mapped[str | None] = mapped_column(Text)
    #: How the typed value was arrived at, when it was not simply copied — "read
    #: “overnight on Friday” as 1 May 2026 22:00, relative to the notification of
    #: 5 May 2026". A date of loss is the field this exists for: it has to become a
    #: timestamp before a claim can be created, and the officer reviewing the
    #: notice is entitled to see both the broker's words and what they were taken
    #: to mean. Null for a value that needs no explanation, which is most of them.
    inference_note: Mapped[str | None] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String(16), default=FieldSource.AI)

    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_documents.id", ondelete="SET NULL")
    )
    #: The passage. `SET NULL` for the reason `fnol_extracted_fields` uses: a
    #: re-index must cost the citation, never the value.
    source_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_document_chunks.id", ondelete="SET NULL")
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    section_label: Mapped[str | None] = mapped_column(String(128))
    #: The phrase the model says it read the value from, and where that phrase
    #: sits in the document's stored text. Offsets are into the *document*, the
    #: same coordinate system the passage's own offsets use.
    quote: Mapped[str | None] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)

    human_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    original_value: Mapped[str | None] = mapped_column(Text)
    modified_by: Mapped[str | None] = mapped_column(String(255))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_reason: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ExtractionRun | None] = relationship(back_populates="values")
    citations: Mapped[list[ExtractedValueCitation]] = relationship(
        back_populates="value",
        cascade="all, delete-orphan",
        order_by="ExtractedValueCitation.rank",
        # Loaded explicitly by the routes that need them. A value row is rendered
        # thirty-eight at a time on the review screen and none of those rows need a
        # citation until somebody clicks one.
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("fnol_case_id", "schema_id", "field_key", name="uq_extracted_value_field"),
        Index("ix_extracted_values_case_schema", "fnol_case_id", "schema_id"),
        CheckConstraint(
            "char_end IS NULL OR char_start IS NULL OR char_end >= char_start",
            name="offsets_ordered",
        ),
    )


class ExtractedValueCitation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One document that states one value, and where in it to look.

    Rows come from two places and the `role` column says which, because a reviewer
    reads them differently:

    * `primary` — a passage the model itself cited. This is where the value was
      *read*, and it is the citation a reviewer is shown first.
    * `corroborating` — a document found to contain the value by searching its
      text after the fact. Nothing about the model's answer depends on it; it is
      evidence that three documents agree, which is a confidence signal in its own
      right and the answer to "does the engineer's report back this up".

    A corroborating row is only ever written when the document's own text actually
    contains the value. A citation pointing at a document that does not state what
    it is cited for is worse than no citation, and inventing one is easy — which is
    why the model's own claim about a second source is not trusted here: it is
    re-derived from the text.
    """

    __tablename__ = "extracted_value_citations"

    value_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extracted_values.id", ondelete="CASCADE"), index=True
    )
    #: `primary` | `corroborating`.
    role: Mapped[str] = mapped_column(String(16), default="corroborating")
    #: Display order within a value: primaries first, then corroborating rows in
    #: the order the documents were ranked. Stored rather than sorted at read time
    #: so the stepper's "source 3 of 7" means the same thing on every request.
    rank: Mapped[int] = mapped_column(Integer, default=0)

    #: `CASCADE` rather than `SET NULL`, unlike the value's own
    #: `source_document_id`: a citation *is* a pointer at a document, so one whose
    #: document has been deleted is not a degraded citation, it is not one.
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_documents.id", ondelete="CASCADE"), index=True
    )
    #: The passage, for a primary citation. Null for a corroborating one: the value
    #: was found in the document's text, which is not cut into passages until it is
    #: indexed, and the offsets are the honest answer either way.
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_document_chunks.id", ondelete="SET NULL")
    )

    page_number: Mapped[int | None] = mapped_column(Integer)
    section_label: Mapped[str | None] = mapped_column(String(128))

    #: The text to mark, **as this document writes it**. A date the notice prints
    #: as "10 January 2026" and the report prints as "2026-01-10" is one value and
    #: two quotes, and highlighting either document with the other's wording finds
    #: nothing.
    quote: Mapped[str | None] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)

    #: Cached rectangles, `[{page_number, x0, top, x1, bottom, page_width,
    #: page_height}]`. Resolved on the first request for *this* citation and kept,
    #: so a reviewer stepping between two documents and back does not re-download
    #: and re-measure a 30MB PDF each time. Null means "not resolved yet"; an empty
    #: list means "resolved, and there is no geometry" — the two are different
    #: answers and `highlight_note` explains the second.
    rects: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
    highlight_note: Mapped[str | None] = mapped_column(Text)

    #: How this citation was arrived at: `chunk-grounded` for a passage the model
    #: cited, `document-search` for one found in the document's text.
    strategy: Mapped[str] = mapped_column(String(24), default="document-search")

    value: Mapped[ExtractedValue] = relationship(back_populates="citations")

    __table_args__ = (
        # One citation per document per value. A value stated four times in one
        # file is still one answer to "does this file state it", and stepping
        # between the four is what the occurrence search is for.
        UniqueConstraint("value_id", "document_id", name="uq_value_citation_document"),
        Index("ix_value_citations_value_rank", "value_id", "rank"),
        CheckConstraint(
            "char_end IS NULL OR char_start IS NULL OR char_end >= char_start",
            name="citation_offsets_ordered",
        ),
        CheckConstraint("role IN ('primary', 'corroborating')", name="citation_role_known"),
    )


__all__ = [
    "ExtractedValue",
    "ExtractedValueCitation",
    "ExtractionRun",
    "ExtractionSchema",
    "ExtractionSchemaField",
]
