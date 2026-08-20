"""The policy library: uploaded wordings and the passages cut from them

Revision ID: 0009_policy_library
Revises: 0008_role_capabilities
Create Date: 2026-08-19

Renumbered from 0008 and re-parented onto `0008_role_capabilities`. Both were written
against 0007 on separate branches, which left Alembic with two heads and `upgrade head`
ambiguous. This one chains after the access-control revision rather than the other way
round, for the same reason that revision chose: `0008_role_capabilities` is already on
main and has been applied by anyone tracking it, so re-parenting *it* would rewrite
history somebody has already run.

Nothing here depends on `role_capabilities` — the order is a linear history rather than
a real dependency, which is why re-parenting was enough and no merge revision was needed.

Two tables, and the decision worth recording is why they are new tables rather than
rows in the ones that already exist.

**`policy_documents` is not `fnol_documents`.** That table's `fnol_case_id` is
`NOT NULL`, and every index, cascade and retrieval filter over it is scoped by case.
A policy wording belongs to no case: it is reference data that outlives every notice
matched against it. Hanging it off a synthetic case would make "delete this notice"
able to delete the policy book, and would put carrier reference data inside the
retention window claim material is destroyed on. Two tables with the same *shape* and
separate lifecycles is the cheaper answer, and the shape is what lets the existing
chunker be reused verbatim.

**`policy_id` is nullable, and that is the interesting column.** A wording is
uploaded before anybody says which row in `policies` it is the wording *for*, and on
a real desk it may never be linked at all — the policy administration system owns
that record, not this service. So the link is an outcome of ingestion rather than a
precondition for it, and `SET NULL` rather than `CASCADE`: removing a policy from the
book must not destroy the document an officer cited last week.

**`checksum_sha256` is unique across the whole library**, unlike
`fnol_documents.checksum_sha256` which is unique only per case. Two brokers sending
the same survey report is two citations of one report; the same wording uploaded
twice is one entry in the book, and a second copy would be retrieved against, scored
and ranked as a rival candidate for the same contract.

`content_tsv` is a generated column with a GIN index, which is what makes matching
degrade to keyword search rather than to nothing when no embedding provider is
configured. Alembic cannot express a generated column declaratively, so it and its
index are declared inline as `sa.Computed`.

Nothing in this revision touches an existing table, so it is safe to apply to a live
deployment and safe to roll back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_policy_library"
down_revision: str | None = "0008_role_capabilities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Both tables carry `updated_at`, maintained by the shared trigger 0001 creates.
_TIMESTAMPED_TABLES = ("policy_documents", "policy_document_chunks")


def upgrade() -> None:
    op.create_table(
        "policy_documents",
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=True),
        sa.Column("extraction_status", sa.String(length=16), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("text_characters", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("text_extractor", sa.String(length=48), nullable=True),
        sa.Column("text_extractor_version", sa.String(length=16), nullable=True),
        sa.Column("page_offsets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # What the document says about itself. Promoted to columns because these are
        # the values the matcher filters and corroborates on; everything else the
        # reader found stays in `extracted_metadata`.
        sa.Column("policy_number", sa.String(length=64), nullable=True),
        sa.Column("insured_name", sa.String(length=255), nullable=True),
        sa.Column("insurer_name", sa.String(length=255), nullable=True),
        sa.Column("broker_name", sa.String(length=255), nullable=True),
        sa.Column("line_of_business", sa.String(length=48), nullable=True),
        sa.Column("policy_type", sa.String(length=96), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("extracted_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Ingestion.
        sa.Column("ingest_status", sa.String(length=16), nullable=False),
        sa.Column("ingest_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("ingest_error", sa.Text(), nullable=True),
        sa.Column("ingest_attempts", sa.Integer(), nullable=False),
        sa.Column("ingest_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedded_chunk_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "ingest_attempts >= 0",
            name=op.f("ck_policy_documents_policy_document_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "chunk_count >= 0",
            name=op.f("ck_policy_documents_policy_document_chunk_count_non_negative"),
        ),
        sa.CheckConstraint(
            "embedded_chunk_count >= 0 AND embedded_chunk_count <= chunk_count",
            name=op.f("ck_policy_documents_policy_document_embedded_count_bounded"),
        ),
        sa.CheckConstraint(
            "expiry_date IS NULL OR effective_date IS NULL OR expiry_date >= effective_date",
            name=op.f("ck_policy_documents_policy_document_period_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name=op.f("fk_policy_documents_policy_id_policies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_documents")),
    )
    op.create_index(
        op.f("ix_policy_documents_checksum_sha256"),
        "policy_documents",
        ["checksum_sha256"],
        unique=True,
    )
    op.create_index(
        op.f("ix_policy_documents_policy_id"), "policy_documents", ["policy_id"], unique=False
    )
    op.create_index(
        op.f("ix_policy_documents_policy_number"),
        "policy_documents",
        ["policy_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_policy_documents_insured_name"),
        "policy_documents",
        ["insured_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_policy_documents_line_of_business"),
        "policy_documents",
        ["line_of_business"],
        unique=False,
    )
    op.create_index(
        op.f("ix_policy_documents_ingest_status"),
        "policy_documents",
        ["ingest_status"],
        unique=False,
    )
    # The two hot list queries: the Policies board ordered by upload time, and the
    # worker sweeping for documents that need attention.
    op.create_index(
        "ix_policy_documents_status_created_at",
        "policy_documents",
        ["ingest_status", "created_at"],
        unique=False,
    )

    op.create_table(
        "policy_document_chunks",
        sa.Column("policy_document_id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_ref", sa.String(length=96), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.Column("page_to", sa.Integer(), nullable=True),
        sa.Column("section_label", sa.String(length=128), nullable=True),
        sa.Column("vector_point_id", sa.UUID(), nullable=True),
        sa.Column("embedding_model", sa.String(length=96), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "char_end >= char_start",
            name=op.f("ck_policy_document_chunks_policy_chunk_offsets_ordered"),
        ),
        sa.CheckConstraint(
            "chunk_index >= 0",
            name=op.f("ck_policy_document_chunks_policy_chunk_index_non_negative"),
        ),
        sa.CheckConstraint(
            "token_count >= 0",
            name=op.f("ck_policy_document_chunks_policy_chunk_token_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_document_id"],
            ["policy_documents.id"],
            name=op.f("fk_policy_document_chunks_policy_document_id_policy_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name=op.f("fk_policy_document_chunks_policy_id_policies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_document_chunks")),
        sa.UniqueConstraint("chunk_ref", name="uq_policy_document_chunk_ref"),
        sa.UniqueConstraint(
            "policy_document_id", "chunk_index", name="uq_policy_document_chunk_index"
        ),
        # Two passages must never claim the same vector, or a search hit resolves to
        # the wrong clause of the wrong policy.
        sa.UniqueConstraint("vector_point_id", name="uq_policy_document_chunk_point"),
    )
    op.create_index(
        op.f("ix_policy_document_chunks_content_hash"),
        "policy_document_chunks",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_policy_document_chunks_policy_document_id"),
        "policy_document_chunks",
        ["policy_document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_policy_document_chunks_policy_id"),
        "policy_document_chunks",
        ["policy_id"],
        unique=False,
    )
    # The keyword half of retrieval. GIN because the query is
    # `content_tsv @@ to_tsquery(...)` across the whole library.
    op.create_index(
        "ix_policy_document_chunks_content_tsv",
        "policy_document_chunks",
        ["content_tsv"],
        unique=False,
        postgresql_using="gin",
    )

    # `set_updated_at()` is created by 0001 and shared by every timestamped table.
    for table in _TIMESTAMPED_TABLES:
        op.execute(
            f"CREATE TRIGGER set_{table}_updated_at"
            f" BEFORE UPDATE ON {table}"
            f" FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in _TIMESTAMPED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS set_{table}_updated_at ON {table}")

    op.drop_index("ix_policy_document_chunks_content_tsv", table_name="policy_document_chunks")
    op.drop_index(
        op.f("ix_policy_document_chunks_policy_id"), table_name="policy_document_chunks"
    )
    op.drop_index(
        op.f("ix_policy_document_chunks_policy_document_id"),
        table_name="policy_document_chunks",
    )
    op.drop_index(
        op.f("ix_policy_document_chunks_content_hash"), table_name="policy_document_chunks"
    )
    op.drop_table("policy_document_chunks")

    op.drop_index("ix_policy_documents_status_created_at", table_name="policy_documents")
    op.drop_index(op.f("ix_policy_documents_ingest_status"), table_name="policy_documents")
    op.drop_index(op.f("ix_policy_documents_line_of_business"), table_name="policy_documents")
    op.drop_index(op.f("ix_policy_documents_insured_name"), table_name="policy_documents")
    op.drop_index(op.f("ix_policy_documents_policy_number"), table_name="policy_documents")
    op.drop_index(op.f("ix_policy_documents_policy_id"), table_name="policy_documents")
    op.drop_index(op.f("ix_policy_documents_checksum_sha256"), table_name="policy_documents")
    op.drop_table("policy_documents")
