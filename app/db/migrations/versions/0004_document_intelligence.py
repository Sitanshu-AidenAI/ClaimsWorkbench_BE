"""Document passages, so an extracted field can cite where it came from

Revision ID: 0004_document_intelligence
Revises: 0003_mail_intake
Create Date: 2026-08-12

One new table and a set of columns, written as Alembic operations in the style
0002 set so `alembic check` keeps them honest against the mapped models.

`fnol_document_chunks` is the table that makes a citation clickable. A passage
carries the page it came from and its character offsets into the document's
extracted text, so "the model read CP-2026-4471 from the survey report" becomes a
highlight on page 4 rather than an assertion. Postgres owns the passage text and
the vector store owns only the vectors: the evidence a claims officer is shown is
part of the claim record, so it has to be readable when the vector store is
unreachable, and the index has to be rebuildable from Postgres alone.

`content_tsv` is a generated column with a GIN index, which is what makes
retrieval degrade to keyword search rather than to nothing when no embedding
provider is configured. Alembic cannot express a generated column declaratively,
so it and its index are raw SQL.

**One-time cost on deploy.** `fnol_documents.extraction_signature` starts NULL, so
every document is re-read once and every case's stored extraction fingerprint is
invalidated once. That is correct rather than wasteful — the readers introduced
alongside this revision genuinely produce different text than the ones they
replaced, and per-page text is what the new columns exist to record — but it is
one free re-extraction per open case and it should not be a surprise on the bill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_document_intelligence"
down_revision: str | None = "0003_mail_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMPED_TABLES = ("fnol_document_chunks",)

def _document_columns() -> tuple[sa.Column, ...]:
    """The columns added to `fnol_documents`, freshly built.

    Built by a function rather than held as a module constant because a `Column`
    is bound to a table by `add_column` and cannot be reused by `drop_column` in
    `downgrade()` afterwards.
    """
    return (
        sa.Column("text_extractor", sa.String(length=48), nullable=True),
        sa.Column("text_extractor_version", sa.String(length=16), nullable=True),
        sa.Column("extraction_signature", sa.String(length=64), nullable=True),
        sa.Column("ocr_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ocr_confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("ocr_reason", sa.String(length=255), nullable=True),
        sa.Column("ocr_pages_processed", sa.Integer(), nullable=True),
        sa.Column("page_offsets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "index_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("index_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("index_error", sa.Text(), nullable=True),
        sa.Column("index_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "embedded_chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
    )


#: The `NOT NULL` additions above carry a server default so existing rows backfill,
#: and the default is dropped immediately afterwards. Every other business column in
#: this schema takes its default from the model in Python, not from Postgres, and
#: leaving these behind would be schema drift `alembic check` is right to fail on.
_BACKFILLED_COLUMNS = (
    "ocr_applied",
    "index_status",
    "index_attempts",
    "chunk_count",
    "embedded_chunk_count",
)


def upgrade() -> None:
    op.create_table(
        "fnol_document_chunks",
        sa.Column("fnol_document_id", sa.UUID(), nullable=False),
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
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
            "char_end >= char_start", name=op.f("ck_fnol_document_chunks_chunk_offsets_ordered")
        ),
        sa.CheckConstraint(
            "chunk_index >= 0", name=op.f("ck_fnol_document_chunks_chunk_index_non_negative")
        ),
        sa.CheckConstraint(
            "token_count >= 0", name=op.f("ck_fnol_document_chunks_chunk_token_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_document_chunks_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fnol_document_id"],
            ["fnol_documents.id"],
            name=op.f("fk_fnol_document_chunks_fnol_document_id_fnol_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_document_chunks")),
        sa.UniqueConstraint("chunk_ref", name="uq_fnol_document_chunk_ref"),
        sa.UniqueConstraint(
            "fnol_document_id", "chunk_index", name="uq_fnol_document_chunk_index"
        ),
        sa.UniqueConstraint("vector_point_id", name="uq_fnol_document_chunk_point"),
    )
    op.create_index(
        op.f("ix_fnol_document_chunks_content_hash"),
        "fnol_document_chunks",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_fnol_document_chunks_fnol_case_id"),
        "fnol_document_chunks",
        ["fnol_case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_fnol_document_chunks_fnol_document_id"),
        "fnol_document_chunks",
        ["fnol_document_id"],
        unique=False,
    )

    # The keyword half of retrieval, GIN-indexed because the query is
    # `content_tsv @@ plainto_tsquery(...)` scoped to one case.
    op.create_index(
        "ix_fnol_document_chunks_content_tsv",
        "fnol_document_chunks",
        ["content_tsv"],
        unique=False,
        postgresql_using="gin",
    )

    for column in _document_columns():
        op.add_column("fnol_documents", column)
    for name in _BACKFILLED_COLUMNS:
        op.alter_column("fnol_documents", name, server_default=None)

    op.create_index(
        op.f("ix_fnol_documents_extraction_signature"),
        "fnol_documents",
        ["extraction_signature"],
        unique=False,
    )
    op.create_index(
        op.f("ix_fnol_documents_index_status"), "fnol_documents", ["index_status"], unique=False
    )
    op.create_check_constraint(
        op.f("ck_fnol_documents_document_index_attempts_non_negative"),
        "fnol_documents",
        "index_attempts >= 0",
    )
    op.create_check_constraint(
        op.f("ck_fnol_documents_document_chunk_count_non_negative"),
        "fnol_documents",
        "chunk_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_fnol_documents_document_embedded_chunk_count_bounded"),
        "fnol_documents",
        "embedded_chunk_count >= 0 AND embedded_chunk_count <= chunk_count",
    )

    # The citation itself. `SET NULL` rather than `CASCADE`: re-indexing a document
    # replaces its passages, and losing a citation must never lose the value it
    # justified — an officer would rather read "CP-2026-4471, source no longer
    # available" than find the field empty.
    op.add_column(
        "fnol_extracted_fields", sa.Column("source_chunk_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_fnol_extracted_fields_source_chunk_id_fnol_document_chunks"),
        "fnol_extracted_fields",
        "fnol_document_chunks",
        ["source_chunk_id"],
        ["id"],
        ondelete="SET NULL",
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

    op.drop_constraint(
        op.f("fk_fnol_extracted_fields_source_chunk_id_fnol_document_chunks"),
        "fnol_extracted_fields",
        type_="foreignkey",
    )
    op.drop_column("fnol_extracted_fields", "source_chunk_id")

    op.drop_constraint(
        op.f("ck_fnol_documents_document_embedded_chunk_count_bounded"),
        "fnol_documents",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_fnol_documents_document_chunk_count_non_negative"),
        "fnol_documents",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_fnol_documents_document_index_attempts_non_negative"),
        "fnol_documents",
        type_="check",
    )
    op.drop_index(op.f("ix_fnol_documents_index_status"), table_name="fnol_documents")
    op.drop_index(op.f("ix_fnol_documents_extraction_signature"), table_name="fnol_documents")
    for column in reversed(_document_columns()):
        op.drop_column("fnol_documents", column.name)

    op.drop_index("ix_fnol_document_chunks_content_tsv", table_name="fnol_document_chunks")
    op.drop_index(
        op.f("ix_fnol_document_chunks_fnol_document_id"), table_name="fnol_document_chunks"
    )
    op.drop_index(op.f("ix_fnol_document_chunks_fnol_case_id"), table_name="fnol_document_chunks")
    op.drop_index(op.f("ix_fnol_document_chunks_content_hash"), table_name="fnol_document_chunks")
    op.drop_table("fnol_document_chunks")
