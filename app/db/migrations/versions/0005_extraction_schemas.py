"""Configurable extraction datasets, runs, and the values they produce

Revision ID: 0005_extraction_schemas
Revises: 0004_document_intelligence
Create Date: 2026-08-13

Four tables that turn "which fields do we read out of a notice" from Python into
configuration. `extraction_schemas` is a named dataset; its fields carry the
prose that is used verbatim as the retrieval query; a run records one execution
against one notice; and `extracted_values` holds the answers with the passage
each was read from.

Nothing existing changes shape. `fnol_extracted_fields` keeps its contract with
the review screen and is kept in step for the built-in FNOL dataset by
`app.services.fnol.adapter` — the two stores exist because they answer different
questions, and merging them would force every future dataset to fit the claim
record's columns.

Two constraints are worth reading rather than skimming:

* **The default-schema index is partial and unique.** Exactly one dataset is the
  one the pipeline runs, and enforcing that in Postgres rather than in a service
  is what stops two concurrent edits from both winning and leaving a desk whose
  notices are read against whichever row a query happened to return first.
* **Both provenance foreign keys are `ON DELETE SET NULL`.** Re-indexing a
  document replaces its passages; a citation must be allowed to lapse, and a
  *value* must never be deleted because the paragraph it came from was re-cut.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_extraction_schemas"
down_revision: str | None = "0004_document_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_schemas",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "review_threshold",
            sa.Numeric(precision=4, scale=3),
            nullable=False,
            server_default=sa.text("0.600"),
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_extraction_schemas_version_positive")
        ),
        sa.CheckConstraint(
            "review_threshold >= 0 AND review_threshold <= 1",
            name=op.f("ck_extraction_schemas_review_threshold_bounded"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_schemas")),
        sa.UniqueConstraint("key", name="uq_extraction_schemas_key"),
    )

    # Exactly one default, enforced by the database. A partial index rather than a
    # unique constraint because `is_default = false` is the common case and must
    # not be constrained at all.
    op.create_index(
        "uq_extraction_schemas_single_default",
        "extraction_schemas",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "extraction_schema_fields",
        sa.Column("schema_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=96), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "data_type", sa.String(length=16), nullable=False, server_default=sa.text("'string'")
        ),
        sa.Column(
            "group_label", sa.String(length=64), nullable=False, server_default=sa.text("'Fields'")
        ),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extraction_hint", sa.Text(), nullable=True),
        sa.Column("query_embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("query_embedding_model", sa.String(length=96), nullable=True),
        sa.Column("query_embedding_hash", sa.String(length=64), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_extraction_schema_fields_field_position_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["extraction_schemas.id"],
            name=op.f("fk_extraction_schema_fields_schema_id_extraction_schemas"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_schema_fields")),
        sa.UniqueConstraint("schema_id", "key", name="uq_extraction_schema_field_key"),
    )
    op.create_index(
        op.f("ix_extraction_schema_fields_schema_id"),
        "extraction_schema_fields",
        ["schema_id"],
        unique=False,
    )

    op.create_table(
        "extraction_runs",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("schema_id", sa.UUID(), nullable=False),
        sa.Column("schema_key", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'queued'")
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("fields_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fields_extracted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "fields_needing_review", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("fields_failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retrieval_strategy", sa.String(length=32), nullable=True),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("chunks_available", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=96), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "fields_total >= 0",
            name=op.f("ck_extraction_runs_fields_total_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_extraction_runs_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["extraction_schemas.id"],
            name=op.f("fk_extraction_runs_schema_id_extraction_schemas"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_runs")),
    )
    op.create_index(
        op.f("ix_extraction_runs_fingerprint"), "extraction_runs", ["fingerprint"], unique=False
    )
    op.create_index(
        op.f("ix_extraction_runs_fnol_case_id"), "extraction_runs", ["fnol_case_id"], unique=False
    )
    op.create_index(
        op.f("ix_extraction_runs_schema_id"), "extraction_runs", ["schema_id"], unique=False
    )
    op.create_index(
        "ix_extraction_runs_case_schema",
        "extraction_runs",
        ["fnol_case_id", "schema_id"],
        unique=False,
    )

    op.create_table(
        "extracted_values",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("schema_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("field_key", sa.String(length=96), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column(
            "group_label", sa.String(length=64), nullable=False, server_default=sa.text("'Fields'")
        ),
        sa.Column(
            "data_type", sa.String(length=16), nullable=False, server_default=sa.text("'string'")
        ),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default=sa.text("'ai'")),
        sa.Column("source_document_id", sa.UUID(), nullable=True),
        sa.Column("source_chunk_id", sa.UUID(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_label", sa.String(length=128), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("rects", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("highlight_note", sa.Text(), nullable=True),
        sa.Column("human_modified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("modified_by", sa.String(length=255), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "char_end IS NULL OR char_start IS NULL OR char_end >= char_start",
            name=op.f("ck_extracted_values_offsets_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_extracted_values_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.id"],
            name=op.f("fk_extracted_values_run_id_extraction_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["extraction_schemas.id"],
            name=op.f("fk_extracted_values_schema_id_extraction_schemas"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_chunk_id"],
            ["fnol_document_chunks.id"],
            name=op.f("fk_extracted_values_source_chunk_id_fnol_document_chunks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["fnol_documents.id"],
            name=op.f("fk_extracted_values_source_document_id_fnol_documents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extracted_values")),
        sa.UniqueConstraint(
            "fnol_case_id", "schema_id", "field_key", name="uq_extracted_value_field"
        ),
    )
    op.create_index(
        op.f("ix_extracted_values_fnol_case_id"), "extracted_values", ["fnol_case_id"], unique=False
    )
    op.create_index(
        op.f("ix_extracted_values_schema_id"), "extracted_values", ["schema_id"], unique=False
    )
    op.create_index(
        "ix_extracted_values_case_schema",
        "extracted_values",
        ["fnol_case_id", "schema_id"],
        unique=False,
    )

    # Every business default in this schema lives in the model, not in Postgres.
    # The server defaults above exist only so the `NOT NULL` columns can be added
    # to a table that may already have rows; leaving them behind is drift that
    # `alembic check` is right to fail on.
    for table, columns in (
        (
            "extraction_schemas",
            ("version", "status", "is_builtin", "is_default", "review_threshold"),
        ),
        (
            "extraction_schema_fields",
            ("data_type", "group_label", "required", "enabled", "position"),
        ),
        (
            "extraction_runs",
            (
                "schema_version",
                "status",
                "fields_total",
                "fields_extracted",
                "fields_needing_review",
                "fields_failed",
                "degraded",
                "chunks_available",
                "llm_calls",
                "latency_ms",
            ),
        ),
        (
            "extracted_values",
            ("group_label", "data_type", "needs_review", "source", "human_modified"),
        ),
    ):
        for column in columns:
            op.alter_column(table, column, server_default=None)


def downgrade() -> None:
    op.drop_table("extracted_values")
    op.drop_table("extraction_runs")
    op.drop_table("extraction_schema_fields")
    op.drop_index("uq_extraction_schemas_single_default", table_name="extraction_schemas")
    op.drop_table("extraction_schemas")
