"""Notification panel: desk-wide notifications and per-person read markers

Revision ID: 0006_notifications
Revises: 0005_extraction_schemas
Create Date: 2026-08-14

Two tables, written as Alembic operations in the style 0002 set, so
`alembic check` keeps them honest against the mapped models.

Three things in here are load-bearing rather than incidental:

**`notifications.dedupe_key` is unique.** Every producer of a notification runs
either on a schedule or under a retry — a re-delivered Celery task, a mailbox
re-polled after a crash — so "emit this event" has to be safe to call twice. A
service-layer check alone loses the race between two workers, which is the same
argument 0003 makes for `mail_intake_messages.graph_message_id`.

**`notification_reads` is a table rather than a column.** A `read_at` on the
notification would make one officer clearing their badge clear it for the whole
desk, which is the failure mode that makes a shared inbox unusable. The absence
of a row here is the unread state, so creating a notification writes nothing to
this table and the common case costs nothing.

**`notifications.fnol_case_id` cascades.** Unlike the mailbox ledger's `SET
NULL`: a panel row that outlived its notice would announce a reference that now
404s, and there is no question a nulled notification usefully answers. The
deletion path counts them before they go so the receipt can report them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_notifications"
down_revision: str | None = "0005_extraction_schemas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMPED_TABLES = (
    "notification_reads",
    "notifications",
)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("tone", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=512), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fnol_case_id", sa.UUID(), nullable=True),
        sa.Column("fnol_reference", sa.String(length=32), nullable=True),
        sa.Column("mail_intake_message_id", sa.UUID(), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_notifications_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mail_intake_message_id"],
            ["mail_intake_messages.id"],
            name=op.f("fk_notifications_mail_intake_message_id_mail_intake_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifications_dedupe_key")),
    )
    op.create_index(op.f("ix_notifications_kind"), "notifications", ["kind"], unique=False)
    op.create_index(
        op.f("ix_notifications_occurred_at"), "notifications", ["occurred_at"], unique=False
    )
    op.create_index(
        op.f("ix_notifications_fnol_case_id"), "notifications", ["fnol_case_id"], unique=False
    )
    op.create_index(
        op.f("ix_notifications_fnol_reference"), "notifications", ["fnol_reference"], unique=False
    )
    # The panel's only ordering. Descending explicitly: the polled list request is
    # always "newest first", and an ascending index serves it by scanning backwards
    # at a cost that grows with the table.
    op.create_index(
        "ix_notifications_occurred_at_desc",
        "notifications",
        [sa.text("occurred_at DESC")],
        unique=False,
    )

    op.create_table(
        "notification_reads",
        sa.Column("notification_id", sa.UUID(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_reads_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_reads")),
        # Named without `op.f` so the service can spell it in `ON CONFLICT`: the
        # marking-read path needs this constraint by name to make a double click a
        # no-op rather than a 500.
        sa.UniqueConstraint("notification_id", "subject", name="uq_notification_read"),
    )
    op.create_index(
        op.f("ix_notification_reads_notification_id"),
        "notification_reads",
        ["notification_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_reads_subject"), "notification_reads", ["subject"], unique=False
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

    op.drop_index(op.f("ix_notification_reads_subject"), table_name="notification_reads")
    op.drop_index(op.f("ix_notification_reads_notification_id"), table_name="notification_reads")
    op.drop_table("notification_reads")

    op.drop_index("ix_notifications_occurred_at_desc", table_name="notifications")
    op.drop_index(op.f("ix_notifications_fnol_reference"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_fnol_case_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_occurred_at"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_kind"), table_name="notifications")
    op.drop_table("notifications")
