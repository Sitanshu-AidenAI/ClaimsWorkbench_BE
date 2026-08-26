"""Every document that states a value, not just the one it was read from

Revision ID: 0011_value_citations
Revises: 0010_value_inference_note
Create Date: 2026-08-21

`extracted_values` can point at exactly one document, because it carries one
`source_document_id`. That was a fair model of "where did we read this" and a poor
model of the question an officer actually asks, which is "who says so" — and on a
claim notification three files routinely say so: the broker's email, the completed
notice form and the engineer's report all print the policy number.

With one column the review screen can only ever open one of the three, and which one
it opens is decided by which passage the model happened to cite. Every other document
that states the same value is invisible, so a corroborated value and an uncorroborated
one look identical on screen — and telling them apart is most of what verifying an
extraction *is*.

So: a child table, one row per document per value, `role` distinguishing the passage
the model read from the documents later found to agree with it.

The two highlight-cache columns move here from `extracted_values` rather than being
duplicated. They cache the geometry of *one* rectangle set on *one* page of *one*
file, which was always a property of a citation rather than of a value; leaving them
on the parent would mean the cache answered for whichever document was asked about
last.

The cached rectangles themselves are **not** carried over — the backfill leaves them
null. They are derived from the stored file by a matcher this release also fixes (a
section heading sharing its first word with the field beneath it used to steal the
match), so a copied cache would be a set of boxes measured by code that no longer
exists, on exactly the documents where it was wrong. Null means "not measured yet",
which costs one re-measure the first time somebody opens each citation and is the only
answer that cannot be stale.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_value_citations"
down_revision: str | None = "0010_value_inference_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extracted_value_citations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Naive, matching `TimestampMixin` — the model maps a bare `datetime`, and a
        # column the ORM thinks is naive while the database makes it aware is the
        # kind of mismatch that only shows up on a comparison months later.
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "value_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("extracted_values.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="corroborating"),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        # CASCADE, unlike `extracted_values.source_document_id`: a value survives
        # losing its citation, but a citation *is* a pointer at a document and does
        # not survive losing it.
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fnol_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fnol_document_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_label", sa.String(length=128), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("rects", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("highlight_note", sa.Text(), nullable=True),
        sa.Column(
            "strategy", sa.String(length=24), nullable=False, server_default="document-search"
        ),
        sa.UniqueConstraint("value_id", "document_id", name="uq_value_citation_document"),
        sa.CheckConstraint(
            "char_end IS NULL OR char_start IS NULL OR char_end >= char_start",
            name="citation_offsets_ordered",
        ),
        sa.CheckConstraint("role IN ('primary', 'corroborating')", name="citation_role_known"),
    )
    op.create_index(
        "ix_extracted_value_citations_value_id", "extracted_value_citations", ["value_id"]
    )
    op.create_index(
        "ix_extracted_value_citations_document_id", "extracted_value_citations", ["document_id"]
    )
    op.create_index(
        "ix_value_citations_value_rank", "extracted_value_citations", ["value_id", "rank"]
    )

    # Backfill the primary citation from the column it is moving out of, so a
    # deployment upgrading with cases already on the review screen keeps every
    # highlight it had. The corroborating rows appear on the next run of each
    # dataset — they need the documents' text, which a migration should not read.
    op.execute(
        """
        INSERT INTO extracted_value_citations (
            value_id, role, rank, document_id, chunk_id, page_number,
            section_label, quote, char_start, char_end, strategy
        )
        SELECT
            v.id,
            'primary',
            0,
            v.source_document_id,
            v.source_chunk_id,
            v.page_number,
            v.section_label,
            v.quote,
            v.char_start,
            v.char_end,
            CASE WHEN v.source_chunk_id IS NULL THEN 'document-search' ELSE 'chunk-grounded' END
        FROM extracted_values v
        WHERE v.source_document_id IS NOT NULL
        """
    )

    op.drop_column("extracted_values", "rects")
    op.drop_column("extracted_values", "highlight_note")


def downgrade() -> None:
    op.add_column(
        "extracted_values",
        sa.Column("rects", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("extracted_values", sa.Column("highlight_note", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE extracted_values v
           SET rects = c.rects, highlight_note = c.highlight_note
        FROM extracted_value_citations c
        WHERE c.value_id = v.id AND c.role = 'primary'
        """
    )
    op.drop_index("ix_value_citations_value_rank", table_name="extracted_value_citations")
    op.drop_index(
        "ix_extracted_value_citations_document_id", table_name="extracted_value_citations"
    )
    op.drop_index("ix_extracted_value_citations_value_id", table_name="extracted_value_citations")
    op.drop_table("extracted_value_citations")
