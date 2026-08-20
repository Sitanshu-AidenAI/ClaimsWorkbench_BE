"""The policy library: an uploaded wording, and the passages cut from it.

Two tables that deliberately mirror `fnol_documents` / `fnol_document_chunks`
rather than reusing them, and the mirroring is the decision worth explaining.

**Why not reuse the FNOL tables.** `fnol_documents.fnol_case_id` is `NOT NULL`
and every index, every cascade and every retrieval filter in that module is
scoped by case. A policy wording belongs to no case: it is reference data that
outlives every notice it is matched against. Hanging it off a synthetic case
would make "delete this notice" able to delete the policy book, and would put
carrier reference data inside the retention window claim material is destroyed
on. Two tables with the same *shape* and separate lifecycles is the cheaper
answer, and the shape is what lets `app/services/intelligence/chunking.py` be
reused verbatim.

**Postgres owns the text; the vector store owns the vectors.** The same division
`FNOLDocumentChunk` documents, and here it buys the same three things: an excerpt
shown to an officer as the reason a policy matched is readable when Qdrant is
unreachable, re-embedding to a new model is "read Postgres, write Qdrant", and
the collection is rebuildable from Postgres alone.

**`policy_id` is nullable, and that is the interesting column.** A wording is
uploaded before anybody says which row in `policies` it is the wording *for*, and
on a real desk it may never be linked at all — the policy administration system
is the owner of that record and this service is not. So the link is an outcome of
ingestion rather than a precondition for it: the number read out of the document
is matched against the book, and when it resolves the document is linked. When it
does not, the document is still ingested, still searchable and still matchable,
and the metadata read from its own first page is what a match is explained with.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import DocumentExtractionStatus, PolicyIngestStatus


class PolicyDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One policy wording as it was uploaded, and how far ingestion got.

    The row is written before any reading happens, which is what makes the upload
    endpoint fast and the ingestion asynchronous: the response says "accepted, and
    here is the id to watch", and the worker moves the status along. Everything an
    administrator needs to answer "what happened to my upload" is a column here —
    the status, the attempt count, the error, and the two counts whose difference
    is the whole story when a vector store is down.
    """

    __tablename__ = "policy_documents"

    #: The book entry this wording belongs to, once it is known. Nullable by
    #: design — see the module docstring. `SET NULL` rather than `CASCADE`:
    #: removing a policy from the book must not destroy the document an officer
    #: cited last week.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policies.id", ondelete="SET NULL"), index=True
    )

    # --- The file -------------------------------------------------------------
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(512))
    #: Unique across the library, unlike `fnol_documents.checksum_sha256` which is
    #: unique only per case. Two brokers sending the same survey report is two
    #: citations of one report; the same policy wording uploaded twice is one
    #: entry in the book, and a second copy would be matched against twice and
    #: ranked as two candidates for the same contract.
    checksum_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(255))

    # --- What could be read out of it ----------------------------------------
    extraction_status: Mapped[str] = mapped_column(
        String(16), default=DocumentExtractionStatus.PENDING
    )
    extracted_text: Mapped[str | None] = mapped_column(Text)
    text_characters: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    text_extractor: Mapped[str | None] = mapped_column(String(48))
    text_extractor_version: Mapped[str | None] = mapped_column(String(16))
    #: `[[start, end], …]` per page into `extracted_text`, written by the reader
    #: that joined them. What makes an excerpt's page number exact.
    page_offsets: Mapped[list[list[int]] | None] = mapped_column(JSONB)

    # --- What the document says about itself ----------------------------------
    # Promoted to columns rather than left in `extracted_metadata`, and only these
    # six, because these are the ones the matcher *filters and corroborates* on. A
    # value is a matching signal once it has a column; everything else the reader
    # found is kept in the JSON bag, shown on screen, and not matched on. The same
    # seam `fnol_cases` draws for the identification engine, for the same reason.
    policy_number: Mapped[str | None] = mapped_column(String(64), index=True)
    insured_name: Mapped[str | None] = mapped_column(String(255), index=True)
    insurer_name: Mapped[str | None] = mapped_column(String(255))
    broker_name: Mapped[str | None] = mapped_column(String(255))
    line_of_business: Mapped[str | None] = mapped_column(String(48), index=True)
    policy_type: Mapped[str | None] = mapped_column(String(96))
    effective_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    #: Everything else the reader found — additional named insureds, scheduled
    #: premises, limits, deductibles, form numbers. Open in shape because it
    #: differs per line of business, and nothing reasons about its internals
    #: beyond presence and display.
    extracted_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # --- Ingestion -----------------------------------------------------------
    ingest_status: Mapped[str] = mapped_column(
        String(16), default=PolicyIngestStatus.PENDING, index=True
    )
    #: `sha256(checksum | extractor | version | chunk params | embedding model)`.
    #: Unchanged and settled means the whole pipeline is skipped, so re-uploading
    #: or re-queueing a document costs one `SELECT`.
    ingest_fingerprint: Mapped[str | None] = mapped_column(String(64))
    ingest_error: Mapped[str | None] = mapped_column(Text)
    ingest_attempts: Mapped[int] = mapped_column(Integer, default=0)
    ingest_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Below `chunk_count` means the vector index is behind the passages. Visible
    #: drift rather than silent: the Policies screen shows both numbers.
    embedded_chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    chunks: Mapped[list[PolicyDocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        #: `lazy="raise"` for the same reason `FNOLDocument.chunks` carries it: a
        #: thirty-page policy is hundreds of passages, and listing the library
        #: must never load them as a side effect of reading a row.
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint("ingest_attempts >= 0", name="policy_document_attempts_non_negative"),
        CheckConstraint("chunk_count >= 0", name="policy_document_chunk_count_non_negative"),
        CheckConstraint(
            "embedded_chunk_count >= 0 AND embedded_chunk_count <= chunk_count",
            name="policy_document_embedded_count_bounded",
        ),
        CheckConstraint(
            "expiry_date IS NULL OR effective_date IS NULL OR expiry_date >= effective_date",
            name="policy_document_period_ordered",
        ),
        # The two hot list queries: the Policies board ordered by upload time, and
        # the worker sweeping for documents that need attention.
        Index("ix_policy_documents_status_created_at", "ingest_status", "created_at"),
    )


class PolicyDocumentChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One passage of one policy wording, and where in the document it is.

    This is what a match is explained *with*. A policy card that says "89%
    confident" and shows nothing is an assertion; one that quotes the two clauses
    the notice's own words retrieved, with their page numbers, is evidence — and
    the offsets here are what turn the second into a highlight on the page.

    `policy_id` is denormalised from the document for the same reason
    `FNOLDocumentChunk.fnol_case_id` is: the hot query is "the passages of the
    policies in this candidate set", and it should not need a join.
    """

    __tablename__ = "policy_document_chunks"

    policy_document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policy_documents.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policies.id", ondelete="SET NULL"), index=True
    )

    chunk_index: Mapped[int] = mapped_column(Integer)
    #: `{document_id}:{chunk_index:05d}` — the same derivation
    #: `app/repositories/chunks.py` uses, so one function names a passage in
    #: Postgres, in the vector payload and in an API response.
    chunk_ref: Mapped[str] = mapped_column(String(96))

    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_count: Mapped[int] = mapped_column(Integer)

    #: Offsets into `policy_documents.extracted_text`. The excerpt is resolved from
    #: these, so they must address the stored text exactly — which is the invariant
    #: `chunking.py` exists to hold.
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)

    page_number: Mapped[int | None] = mapped_column(Integer)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    section_label: Mapped[str | None] = mapped_column(String(128))

    #: `uuid5` of the chunk ref, so re-ingesting overwrites in place.
    vector_point_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    embedding_model: Mapped[str | None] = mapped_column(String(96))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The keyword half of retrieval. Generated by Postgres so it cannot drift from
    #: `content`, and it is what makes policy matching work at all on a deployment
    #: with no embedding provider — which matters more here than on the claim side,
    #: because a policy number and an insured name are exactly the kind of rare
    #: token full text is best at.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        deferred=True,
    )

    document: Mapped[PolicyDocument] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_policy_document_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        UniqueConstraint(
            "policy_document_id", "chunk_index", name="uq_policy_document_chunk_index"
        ),
        UniqueConstraint("chunk_ref", name="uq_policy_document_chunk_ref"),
        # Two passages must never claim the same vector, or a search hit resolves to
        # the wrong clause of the wrong policy.
        UniqueConstraint("vector_point_id", name="uq_policy_document_chunk_point"),
        CheckConstraint("char_end >= char_start", name="policy_chunk_offsets_ordered"),
        CheckConstraint("chunk_index >= 0", name="policy_chunk_index_non_negative"),
        CheckConstraint("token_count >= 0", name="policy_chunk_token_count_non_negative"),
    )


__all__ = ["PolicyDocument", "PolicyDocumentChunk"]
