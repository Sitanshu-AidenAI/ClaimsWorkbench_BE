"""Mailbox intake ledger: collected messages and their attachments

Revision ID: 0003_mail_intake
Revises: 0002_fnol_module
Create Date: 2026-08-12

Two tables, written as Alembic operations in the style 0002 set, so
`alembic check` keeps them honest against the mapped models.

The uniqueness on `graph_message_id` and `internet_message_id` is the point of
the revision: it is what makes redelivery of a broker's email cost a query
rather than a duplicate claim notification. Both are database constraints rather
than a service-layer check, because two workers polling the same mailbox at the
same moment is a race no amount of application code wins on its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_mail_intake"
down_revision: str | None = "0002_fnol_module"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMPED_TABLES = (
    "mail_intake_attachments",
    "mail_intake_messages",
)


def upgrade() -> None:
    op.create_table(
        "mail_intake_messages",
        sa.Column("mailbox", sa.String(length=320), nullable=False),
        sa.Column("graph_message_id", sa.String(length=512), nullable=False),
        sa.Column("internet_message_id", sa.String(length=998), nullable=True),
        sa.Column("conversation_id", sa.String(length=512), nullable=True),
        sa.Column("subject", sa.String(length=998), nullable=True),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("sender_address", sa.String(length=320), nullable=True),
        sa.Column("to_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cc_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), nullable=False),
        sa.Column("attachment_count", sa.Integer(), nullable=False),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("body_content", sa.Text(), nullable=True),
        sa.Column("body_content_type", sa.String(length=16), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("marked_read", sa.Boolean(), nullable=False),
        sa.Column("moved_to_folder", sa.String(length=255), nullable=True),
        sa.Column("fnol_case_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_mail_intake_messages_mail_attempts_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_mail_intake_messages_fnol_case_id_fnol_cases"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_intake_messages")),
        sa.UniqueConstraint(
            "graph_message_id", name=op.f("uq_mail_intake_messages_graph_message_id")
        ),
        sa.UniqueConstraint(
            "internet_message_id", name=op.f("uq_mail_intake_messages_internet_message_id")
        ),
    )
    op.create_index(
        op.f("ix_mail_intake_messages_conversation_id"),
        "mail_intake_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_intake_messages_fnol_case_id"),
        "mail_intake_messages",
        ["fnol_case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_intake_messages_mailbox"), "mail_intake_messages", ["mailbox"], unique=False
    )
    op.create_index(
        op.f("ix_mail_intake_messages_received_at"),
        "mail_intake_messages",
        ["received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_intake_messages_sender_address"),
        "mail_intake_messages",
        ["sender_address"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_intake_messages_status"), "mail_intake_messages", ["status"], unique=False
    )
    # The poll's own query: "what is outstanding, oldest first".
    op.create_index(
        "ix_mail_intake_messages_status_received_at",
        "mail_intake_messages",
        ["status", "received_at"],
        unique=False,
    )

    op.create_table(
        "mail_intake_attachments",
        sa.Column("mail_intake_message_id", sa.UUID(), nullable=False),
        sa.Column("graph_attachment_id", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("is_inline", sa.Boolean(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("fnol_document_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name=op.f("ck_mail_intake_attachments_mail_attachment_size_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["fnol_document_id"],
            ["fnol_documents.id"],
            name=op.f("fk_mail_intake_attachments_fnol_document_id_fnol_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["mail_intake_message_id"],
            ["mail_intake_messages.id"],
            name=op.f("fk_mail_intake_attachments_mail_intake_message_id_mail_intake_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_intake_attachments")),
        sa.UniqueConstraint(
            "mail_intake_message_id", "graph_attachment_id", name="uq_mail_intake_attachment"
        ),
    )
    op.create_index(
        op.f("ix_mail_intake_attachments_checksum_sha256"),
        "mail_intake_attachments",
        ["checksum_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_intake_attachments_mail_intake_message_id"),
        "mail_intake_attachments",
        ["mail_intake_message_id"],
        unique=False,
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

    op.drop_index(
        op.f("ix_mail_intake_attachments_mail_intake_message_id"),
        table_name="mail_intake_attachments",
    )
    op.drop_index(
        op.f("ix_mail_intake_attachments_checksum_sha256"), table_name="mail_intake_attachments"
    )
    op.drop_table("mail_intake_attachments")

    op.drop_index("ix_mail_intake_messages_status_received_at", table_name="mail_intake_messages")
    op.drop_index(op.f("ix_mail_intake_messages_status"), table_name="mail_intake_messages")
    op.drop_index(op.f("ix_mail_intake_messages_sender_address"), table_name="mail_intake_messages")
    op.drop_index(op.f("ix_mail_intake_messages_received_at"), table_name="mail_intake_messages")
    op.drop_index(op.f("ix_mail_intake_messages_mailbox"), table_name="mail_intake_messages")
    op.drop_index(op.f("ix_mail_intake_messages_fnol_case_id"), table_name="mail_intake_messages")
    op.drop_index(
        op.f("ix_mail_intake_messages_conversation_id"), table_name="mail_intake_messages"
    )
    op.drop_table("mail_intake_messages")
