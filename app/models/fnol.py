"""The FNOL case and everything that hangs off it.

The shape of this schema is the module's central decision, so it is worth stating
plainly: an FNOL is *not* one row with a JSON blob in it.

* The normalised claim data lives in typed columns on `fnol_cases`, because the
  intake queue filters, sorts and reports on it.
* The provenance of each of those values — was it read by a model, typed by an
  officer, or supplied by the channel; from which document; with what confidence
  — lives one row per field in `fnol_extracted_fields`. That table is what makes
  "who changed the date of loss and what did it say before" answerable.
* Everything an analysis produced lives in `fnol_ai_analyses`, keyed by kind and
  fingerprinted by its input, so nothing is recomputed while its inputs hold
  still and nothing is lost when they change.

JSON is used where the shape is genuinely open — an email envelope's headers, the
factors behind a severity score — and nowhere else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import (
    AnalysisStatus,
    DocumentExtractionStatus,
    DocumentIndexStatus,
    DuplicateResolution,
    ExceptionStatus,
    FieldSource,
    FNOLStatus,
    PolicyConfidence,
    PolicyIdentificationStatus,
    PolicyMatchStrength,
    PolicyPeriodOutcome,
    ProcessingState,
)


class FNOLCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One first notice of loss, from arrival to the claim it becomes."""

    __tablename__ = "fnol_cases"

    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=FNOLStatus.RECEIVED, index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)

    processing_state: Mapped[str] = mapped_column(
        String(16), default=ProcessingState.IDLE, index=True
    )
    processing_error: Mapped[str | None] = mapped_column(Text)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # --- Ingest identity -----------------------------------------------------
    # Three separate uniqueness guarantees, because there are three ways the same
    # notification can arrive twice: a client retrying a POST, a mailbox
    # redelivering a message, and a broker resending under the same reference.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    message_id: Mapped[str | None] = mapped_column(String(512), unique=True)
    thread_id: Mapped[str | None] = mapped_column(String(512), index=True)
    external_reference: Mapped[str | None] = mapped_column(String(128), index=True)
    source_reference: Mapped[str | None] = mapped_column(String(128))

    #: The envelope as it arrived — sender, recipient, subject, headers, portal
    #: payload. Retained after claim creation; the original notice is evidence.
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_body: Mapped[str | None] = mapped_column(Text)

    # --- Notification --------------------------------------------------------
    reporter_name: Mapped[str | None] = mapped_column(String(255))
    reporter_organisation: Mapped[str | None] = mapped_column(String(255))
    reporter_role: Mapped[str | None] = mapped_column(String(96))
    reporter_email: Mapped[str | None] = mapped_column(String(255))
    reporter_phone: Mapped[str | None] = mapped_column(String(64))

    # --- Policy --------------------------------------------------------------
    policy_number: Mapped[str | None] = mapped_column(String(64), index=True)
    insured_name: Mapped[str | None] = mapped_column(String(255), index=True)
    insured_organisation: Mapped[str | None] = mapped_column(String(255))
    policy_type: Mapped[str | None] = mapped_column(String(64))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policies.id", ondelete="SET NULL"), index=True
    )
    #: True only once a human has accepted the match. Nothing downstream treats an
    #: unconfirmed match as authoritative.
    policy_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Identification signals ---------------------------------------------
    # Columns rather than a JSON bag, because the identification engine reads
    # `fnol_cases` and nothing else. Promoting an extracted value to a column is
    # what makes it a matching signal — the seam is deliberate, so "which fields
    # matter for identifying a policy" has a structural answer rather than a
    # conventional one.
    #
    # `broker_name` is held apart from `reporter_organisation`: the reporter is
    # whoever sent the email, which on a forwarded chain is often the insured's
    # own risk manager, and the broker is named in the signature block.
    broker_name: Mapped[str | None] = mapped_column(String(255))
    broker_reference: Mapped[str | None] = mapped_column(String(128), index=True)
    #: The address of the risk, which on a liability or construction notice is not
    #: the address the loss happened at.
    risk_location: Mapped[str | None] = mapped_column(Text)
    #: Extracted separately from the address, because in the UK it is the single
    #: most discriminating token an address contains.
    loss_postcode: Mapped[str | None] = mapped_column(String(16), index=True)
    project_name: Mapped[str | None] = mapped_column(String(255))
    contract_number: Mapped[str | None] = mapped_column(String(128), index=True)
    #: The period as printed *on the notice*, for corroborating a fuzzy policy
    #: number: "the number is close and the period agrees" is a real inference.
    policy_period_stated: Mapped[str | None] = mapped_column(String(128))

    # --- Policy identification decision --------------------------------------
    #: Where identification has got to, as its own axis. `policy_confirmed` says
    #: whether a policy was bound; this says whether the *question* is settled,
    #: which is not the same thing — a notice can be referred with no policy, and
    #: that is a decision rather than an absence of one.
    policy_identification_status: Mapped[str] = mapped_column(
        String(24), default=PolicyIdentificationStatus.NOT_RUN, index=True
    )
    policy_identification_ran_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why no policy could be identified, when an officer referred it.
    policy_referral_reason: Mapped[str | None] = mapped_column(Text)
    policy_referred_by: Mapped[str | None] = mapped_column(String(255))
    policy_confirmed_by: Mapped[str | None] = mapped_column(String(255))
    policy_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Classification ------------------------------------------------------
    line_of_business: Mapped[str | None] = mapped_column(String(48), index=True)
    claim_type: Mapped[str | None] = mapped_column(String(64))
    loss_type: Mapped[str | None] = mapped_column(String(64))
    complexity: Mapped[str | None] = mapped_column(String(24))
    classification_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    classification_overridden: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Loss ----------------------------------------------------------------
    date_of_loss: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    loss_location: Mapped[str | None] = mapped_column(Text)
    loss_country: Mapped[str | None] = mapped_column(String(64))
    loss_latitude: Mapped[float | None] = mapped_column(Numeric(9, 6, asdecimal=False))
    loss_longitude: Mapped[float | None] = mapped_column(Numeric(9, 6, asdecimal=False))
    loss_description: Mapped[str | None] = mapped_column(Text)
    cause_of_loss: Mapped[str | None] = mapped_column(String(255))
    affected_assets: Mapped[str | None] = mapped_column(Text)
    injuries: Mapped[int | None] = mapped_column(Integer)
    fatalities: Mapped[int | None] = mapped_column(Integer)
    #: Tri-state on purpose. `True` is a stated exposure, `False` is a stated
    #: absence, and `None` is a question nobody has answered — which on a notice
    #: where extraction recovered 24 of 30 fields is the ordinary case, not the
    #: edge one. Collapsing the last two into `false` is how an unassessed
    #: pollution exposure scored identically to an assessed absence.
    business_interruption: Mapped[bool | None] = mapped_column(Boolean)
    structural_damage: Mapped[bool | None] = mapped_column(Boolean)
    environmental_exposure: Mapped[bool | None] = mapped_column(Boolean)

    # --- Financial -----------------------------------------------------------
    estimated_loss_minor: Mapped[int | None] = mapped_column(BigInteger)
    repair_estimate_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")

    # --- Additional ----------------------------------------------------------
    police_reference: Mapped[str | None] = mapped_column(String(128))
    incident_reference: Mapped[str | None] = mapped_column(String(128))
    authorities_involved: Mapped[str | None] = mapped_column(Text)
    #: Tri-state, for the reason given on `business_interruption` above.
    potential_litigation: Mapped[bool | None] = mapped_column(Boolean)

    # --- Assessments (latest values, denormalised so the queue can sort) ------
    severity: Mapped[str | None] = mapped_column(String(16), index=True)
    severity_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    severity_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    fraud_risk: Mapped[str | None] = mapped_column(String(16), index=True)
    fraud_score: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    coverage_indicator: Mapped[str | None] = mapped_column(String(32))
    completeness_score: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))

    cat_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cat_events.id", ondelete="SET NULL"), index=True
    )
    cat_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    cat_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Ownership and outcome ----------------------------------------------
    created_by: Mapped[str | None] = mapped_column(String(255))
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    #: The claim this notice became. `use_alter` because `claims.fnol_case_id`
    #: points back here — the two tables reference each other, and the constraint
    #: has to be added after both exist.
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "claims.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_fnol_cases_claim_id_claims",
        ),
        unique=True,
    )

    documents: Mapped[list[FNOLDocument]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    fields: Mapped[list[FNOLExtractedField]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    parties: Mapped[list[FNOLParty]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    policy_matches: Mapped[list[FNOLPolicyMatch]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    duplicates: Mapped[list[FNOLDuplicateCandidate]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="FNOLDuplicateCandidate.fnol_case_id",
    )
    exceptions: Mapped[list[FNOLException]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    analyses: Mapped[list[FNOLAIAnalysis]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    notes: Mapped[list[FNOLNote]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("injuries IS NULL OR injuries >= 0", name="injuries_non_negative"),
        CheckConstraint("fatalities IS NULL OR fatalities >= 0", name="fatalities_non_negative"),
        CheckConstraint(
            "estimated_loss_minor IS NULL OR estimated_loss_minor >= 0",
            name="estimated_loss_non_negative",
        ),
        Index("ix_fnol_cases_status_received_at", "status", "received_at"),
    )


class FNOLDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A file attached to the notice, and what could be read out of it.

    The bytes live in object storage under `storage_key`; only the metadata and
    the extracted text are held here, so listing a case never loads a 30MB
    schedule of values.
    """

    __tablename__ = "fnol_documents"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(512))
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)

    #: `email_attachment`, `upload`, `portal`.
    source: Mapped[str] = mapped_column(String(32), default="upload")
    document_kind: Mapped[str] = mapped_column(String(32), default="document")

    extraction_status: Mapped[str] = mapped_column(
        String(16), default=DocumentExtractionStatus.PENDING
    )
    extracted_text: Mapped[str | None] = mapped_column(Text)
    text_characters: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(String(255))

    # --- How the text was read -----------------------------------------------
    #: Which reader produced `extracted_text` — `pdf_text_layer`, `pdf_ocr`, `docx`,
    #: `xlsx`, `eml`, `msg`. Not the content type: a PDF can be read from its text
    #: layer or by OCR, and those are different answers that need re-reading
    #: separately when the reader improves.
    text_extractor: Mapped[str | None] = mapped_column(String(48))
    text_extractor_version: Mapped[str | None] = mapped_column(String(16))
    #: `sha256(checksum | extractor | version)`. Unchanged means the text does not
    #: need re-reading, which is what makes a re-run free rather than another OCR bill.
    extraction_signature: Mapped[str | None] = mapped_column(String(64), index=True)

    ocr_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    #: The detector's own sentence, positive or negative — "avg chars/page 12.4 below
    #: threshold 50". Kept so "why was this OCR'd" is answerable without re-running it.
    ocr_reason: Mapped[str | None] = mapped_column(String(255))
    ocr_pages_processed: Mapped[int | None] = mapped_column(Integer)

    #: `[[start, end], …]` per page into `extracted_text`. Written by the reader that
    #: did the joining, so a page number on a citation is exact rather than estimated.
    page_offsets: Mapped[list[list[int]] | None] = mapped_column(JSONB)

    # --- Indexing ------------------------------------------------------------
    index_status: Mapped[str] = mapped_column(
        String(16), default=DocumentIndexStatus.PENDING, index=True
    )
    #: `sha256(extraction_signature | chunk params | embedding model | dimension)`.
    #: Unchanged and `indexed` means chunking, embedding and the vector upsert are
    #: all skipped.
    index_fingerprint: Mapped[str | None] = mapped_column(String(64))
    index_error: Mapped[str | None] = mapped_column(Text)
    index_attempts: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Below `chunk_count` means the vector index is behind the passages — visible
    #: drift rather than silent, and repaired by re-indexing.
    embedded_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[FNOLCase] = relationship(back_populates="documents")
    #: `lazy="raise"` on purpose. A case can carry thousands of chunks and the
    #: review screen must never load them as a side effect of reading a document
    #: row; everything that wants chunks asks the repository for the ones it needs.
    chunks: Mapped[list[FNOLDocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    __table_args__ = (
        # The same attachment arriving twice on one case is one document. Across
        # cases it is two, because two brokers sending the same survey report is
        # not a duplicate document — it is two notices citing one report.
        UniqueConstraint("fnol_case_id", "checksum_sha256", name="uq_fnol_document_checksum"),
        CheckConstraint("index_attempts >= 0", name="document_index_attempts_non_negative"),
        CheckConstraint("chunk_count >= 0", name="document_chunk_count_non_negative"),
        CheckConstraint(
            "embedded_chunk_count >= 0 AND embedded_chunk_count <= chunk_count",
            name="document_embedded_chunk_count_bounded",
        ),
    )


class FNOLDocumentChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One passage of one document, and where in the document it is.

    This table is what makes a citation clickable. The model is asked to name the
    passage it read a value from; that name resolves to a row here; and the row
    carries the page and the character offsets that turn "the survey report says
    CP-2026-4471" into a highlight on page 4.

    **Postgres owns the text, Qdrant owns the vectors.** The passage a claims
    officer is shown as evidence is part of the claim record: it has to survive the
    vector store being wiped, reindexed or unreachable, and it has to be readable
    in the same transaction as the field row it justifies. Keeping the text here
    also makes the vector index rebuildable from Postgres alone, which turns a lost
    Qdrant volume from a data loss into a re-index.

    `fnol_case_id` is denormalised — it is reachable through `fnol_document_id` —
    because the two hot queries are case-scoped: searching a case's passages and
    reading the evidence behind one of its fields. Neither should need a join.
    """

    __tablename__ = "fnol_document_chunks"

    fnol_document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_documents.id", ondelete="CASCADE"), index=True
    )
    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )

    chunk_index: Mapped[int] = mapped_column(Integer)
    #: `{document_id}:{chunk_index:05d}`. The stable public name of a passage, and
    #: the string the model echoes back to cite it.
    chunk_ref: Mapped[str] = mapped_column(String(96))

    content: Mapped[str] = mapped_column(Text)
    #: sha256 of the whitespace-normalised lowercased text. Two documents sharing a
    #: boilerplate paragraph embed once.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_count: Mapped[int] = mapped_column(Integer)

    #: Offsets into `fnol_documents.extracted_text`. The highlight is resolved from
    #: these, so they must address the stored text exactly.
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)

    page_number: Mapped[int | None] = mapped_column(Integer)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    #: A worksheet name, the nearest heading, a forwarded message's subject. What a
    #: citation shows when the format has no page numbers to show.
    section_label: Mapped[str | None] = mapped_column(String(128))

    #: `uuid5` of the chunk ref, so re-indexing overwrites in place rather than
    #: adding a second copy of every passage. NULL means not in the vector index.
    vector_point_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    embedding_model: Mapped[str | None] = mapped_column(String(96))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The keyword half of retrieval, and what makes it degrade to something real
    #: rather than to nothing when no embedding provider is configured. Generated by
    #: Postgres rather than maintained by the application, so it cannot drift from
    #: `content`; deferred because nothing in Python ever wants to read a tsvector.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        deferred=True,
    )

    document: Mapped[FNOLDocument] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_fnol_document_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        UniqueConstraint("fnol_document_id", "chunk_index", name="uq_fnol_document_chunk_index"),
        UniqueConstraint("chunk_ref", name="uq_fnol_document_chunk_ref"),
        # Two passages must never claim the same vector, or a search hit resolves to
        # the wrong page of the wrong file.
        UniqueConstraint("vector_point_id", name="uq_fnol_document_chunk_point"),
        CheckConstraint("char_end >= char_start", name="chunk_offsets_ordered"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("token_count >= 0", name="chunk_token_count_non_negative"),
    )


class FNOLExtractedField(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The provenance of one value on the case.

    One row per field path, updated in place. `original_value` and
    `human_modified` are what let the review screen draw the three states the
    design calls for — AI extracted, manually entered, corrected by a user —
    without the officer's correction destroying what the model actually said.
    """

    __tablename__ = "fnol_extracted_fields"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(96))
    section: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128))

    value_text: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    source: Mapped[str] = mapped_column(String(16), default=FieldSource.AI)

    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_documents.id", ondelete="SET NULL")
    )
    #: The passage the value was read from. `SET NULL` rather than `CASCADE`: losing
    #: the citation when a document is re-indexed must not lose the value, and an
    #: officer would rather see "CP-2026-4471, source no longer available" than
    #: nothing at all.
    source_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_document_chunks.id", ondelete="SET NULL")
    )
    evidence_snippet: Mapped[str | None] = mapped_column(Text)

    human_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    original_value: Mapped[str | None] = mapped_column(Text)
    modified_by: Mapped[str | None] = mapped_column(String(255))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_reason: Mapped[str | None] = mapped_column(Text)

    case: Mapped[FNOLCase] = relationship(back_populates="fields")

    __table_args__ = (UniqueConstraint("fnol_case_id", "field_path", name="uq_fnol_field_path"),)


class FNOLParty(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Someone involved: claimant, insured, broker, third party, witness, authority."""

    __tablename__ = "fnol_parties"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    organisation: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default=FieldSource.AI)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))

    case: Mapped[FNOLCase] = relationship(back_populates="parties")


class FNOLPolicyMatch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A candidate policy, its score, and what it was scored on.

    Candidates are kept rather than collapsed to a winner: an officer choosing
    between two policies needs to see the one they rejected, and an auditor needs
    to see that a choice existed at all.
    """

    __tablename__ = "fnol_policy_matches"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policies.id", ondelete="CASCADE")
    )
    #: The coarse band the queue and the exception engine read.
    match_strength: Mapped[str] = mapped_column(String(16), default=PolicyMatchStrength.POSSIBLE)
    #: The five-band judgement the identification screen shows. Finer than
    #: `match_strength` on purpose: `weak` is a candidate worth showing below the
    #: line, and `rejected` is one that was compared and failed, which is a
    #: different statement from one that was never compared at all.
    confidence: Mapped[str] = mapped_column(String(16), default=PolicyConfidence.POSSIBLE)
    score: Mapped[float] = mapped_column(Numeric(5, 4, asdecimal=False), default=0.0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    #: `{"policy_number": 1.0, "insured_name": 0.82, ...}` — the per-signal scores.
    #: Kept as the flat form the queue and the audit trail read.
    matched_on: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    #: The per-signal working in full: outcome, weight, both sides' values, the
    #: sentence, and the dataset field the notice's value was read from. This is
    #: what the candidate card renders, and it is stored rather than recomputed so
    #: that what an officer saw when they bound a policy is recoverable later.
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    #: Coverage plausibility, kept apart from the signals. A candidate can be
    #: certainly the right policy and a poor fit for this loss at the same time.
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    #: The card's face, formatted once on the server: insured, period, limit,
    #: excess and the *matched* location, which is the one the excess attaches to.
    display: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    #: `in_force` | `in_maintenance_period` | `prior_term` | `outside_period` |
    #: `unknown`. Five outcomes because property and construction both need them.
    period_outcome: Mapped[str] = mapped_column(String(24), default=PolicyPeriodOutcome.UNKNOWN)
    reasoning: Mapped[str | None] = mapped_column(Text)
    #: How this candidate came to be on the case: ranked by the engine, found by an
    #: officer searching the book, or listed below the threshold as a near miss.
    #: A manually-found policy is still persisted as a candidate before it can be
    #: bound, which is what keeps the selection rule — a bound policy is always one
    #: of this case's candidates — true without making manual search a dead end.
    origin: Mapped[str] = mapped_column(String(24), default="engine")
    #: The engine put this one forward. Never the same thing as `selected`.
    recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    selected_by: Mapped[str | None] = mapped_column(String(255))
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    engine_version: Mapped[str | None] = mapped_column(String(48))

    case: Mapped[FNOLCase] = relationship(back_populates="policy_matches")

    __table_args__ = (
        UniqueConstraint("fnol_case_id", "policy_id", name="uq_fnol_policy_candidate"),
        Index("ix_fnol_policy_matches_case_rank", "fnol_case_id", "rank"),
    )


class FNOLDuplicateCandidate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A record this notice might be a repeat of.

    Never merged and never deleted automatically — the resolution column records
    what a human decided, and `unresolved` is the state that holds the case.
    """

    __tablename__ = "fnol_duplicate_candidates"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    #: `fnol` or `claim` — the two things a notice can repeat.
    candidate_kind: Mapped[str] = mapped_column(String(16))
    candidate_fnol_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE")
    )
    candidate_claim_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    candidate_reference: Mapped[str] = mapped_column(String(32))

    score: Mapped[float] = mapped_column(Numeric(5, 4, asdecimal=False), default=0.0)
    #: `[{"signal": "same_policy", "detail": "POL-2026-0041", "score": 1.0}, ...]`
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    resolution: Mapped[str] = mapped_column(String(16), default=DuplicateResolution.UNRESOLVED)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    case: Mapped[FNOLCase] = relationship(back_populates="duplicates", foreign_keys=[fnol_case_id])

    __table_args__ = (
        UniqueConstraint("fnol_case_id", "candidate_reference", name="uq_fnol_duplicate_candidate"),
        CheckConstraint(
            "(candidate_kind = 'fnol' AND candidate_fnol_id IS NOT NULL)"
            " OR (candidate_kind = 'claim' AND candidate_claim_id IS NOT NULL)",
            name="duplicate_candidate_target",
        ),
    )


class FNOLException(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One thing that needs the officer's attention.

    Upserted by code, so a re-run of the pipeline updates the detail of an open
    exception rather than stacking a second copy of it — and leaves a resolved one
    resolved unless its cause returns.
    """

    __tablename__ = "fnol_exceptions"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(Text)
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    status: Mapped[str] = mapped_column(String(16), default=ExceptionStatus.OPEN, index=True)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[FNOLCase] = relationship(back_populates="exceptions")

    __table_args__ = (UniqueConstraint("fnol_case_id", "code", name="uq_fnol_exception_code"),)


class FNOLAIAnalysis(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The record of one analysis run.

    `input_fingerprint` is the whole point: the pipeline hashes the inputs an
    analysis depends on, and skips the run when the fingerprint has not moved.
    That is what stops a page refresh costing a language-model call, and what
    makes "why does this case say severity high" answerable after the fact.
    """

    __tablename__ = "fnol_ai_analyses"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(96))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4, asdecimal=False))
    status: Mapped[str] = mapped_column(String(16), default=AnalysisStatus.COMPLETED)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    case: Mapped[FNOLCase] = relationship(back_populates="analyses")

    __table_args__ = (UniqueConstraint("fnol_case_id", "kind", name="uq_fnol_analysis_kind"),)


class FNOLNote(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A note an officer left on the case. Plain text; never rendered as markup."""

    __tablename__ = "fnol_notes"

    fnol_case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("fnol_cases.id", ondelete="CASCADE"), index=True
    )
    author: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)

    case: Mapped[FNOLCase] = relationship(back_populates="notes")


__all__ = [
    "FNOLAIAnalysis",
    "FNOLCase",
    "FNOLDocument",
    "FNOLDuplicateCandidate",
    "FNOLException",
    "FNOLExtractedField",
    "FNOLNote",
    "FNOLParty",
    "FNOLPolicyMatch",
]
