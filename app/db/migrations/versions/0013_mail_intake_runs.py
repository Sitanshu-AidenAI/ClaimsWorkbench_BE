"""Record every mailbox poll, so a poll that never happened is visible

Revision ID: 0013_mail_intake_runs
Revises: 0012_exposure_flags_tri_state
Create Date: 2026-08-27

Mailbox intake had every detector it needed and all of them in the wrong process.
`_reconcile` counts listed messages against ledger rows, `sweep_blind` catches a
folder reporting unread mail a sweep returned nothing for, `backlog_detected`
compares the folder against the ledger — and all three run *inside* a poll. When
no poll runs, every one of them is silent, and a dead scheduler is
indistinguishable from a quiet mailbox: no rows, no errors, `/health` says ok.

That is not hypothetical. On this database, messages received on 21 and 24 August
were collected on the 27th, in a catch-up sweep triggered by hand — up to five
days and twenty-two hours after they arrived. Nothing anywhere had said so.

`mail_intake_runs` is one small row per poll attempt, written whether the attempt
succeeded or failed. It makes "when did intake last run" a question the database
answers, which is what lets a process that is *not* the poller — the API, which
is always up — notice the poller is gone. A watchdog inside the thing it watches
cannot report that thing being dead.

The table is append-only and carries no message content. At the aggressive end of
the configured poll interval it grows by a few thousand rows a day, which is why
the repository has a `prune`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_mail_intake_runs"
down_revision: str | None = "0012_exposure_flags_tri_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_intake_runs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("mailbox", sa.String(length=320), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("swept_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched", sa.Integer(), nullable=False),
        sa.Column("ingested", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("abandoned", sa.Integer(), nullable=False),
        sa.Column("dropped", sa.Integer(), nullable=False),
        sa.Column("folder_total", sa.Integer(), nullable=True),
        sa.Column("folder_unread", sa.Integer(), nullable=True),
        sa.Column("ledger_total", sa.Integer(), nullable=True),
        sa.Column("sweep_blind", sa.Boolean(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "fetched >= 0", name=op.f("ck_mail_intake_runs_mail_run_fetched_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_intake_runs")),
    )
    op.create_index(
        op.f("ix_mail_intake_runs_mailbox"), "mail_intake_runs", ["mailbox"], unique=False
    )
    op.create_index(
        op.f("ix_mail_intake_runs_trigger"), "mail_intake_runs", ["trigger"], unique=False
    )
    op.create_index(
        op.f("ix_mail_intake_runs_started_at"), "mail_intake_runs", ["started_at"], unique=False
    )
    op.create_index(op.f("ix_mail_intake_runs_ok"), "mail_intake_runs", ["ok"], unique=False)
    # The health check's only query: the newest run for one mailbox.
    op.create_index(
        "ix_mail_intake_runs_mailbox_started_at",
        "mail_intake_runs",
        ["mailbox", sa.text("started_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_intake_runs_mailbox_started_at", table_name="mail_intake_runs")
    op.drop_index(op.f("ix_mail_intake_runs_ok"), table_name="mail_intake_runs")
    op.drop_index(op.f("ix_mail_intake_runs_started_at"), table_name="mail_intake_runs")
    op.drop_index(op.f("ix_mail_intake_runs_trigger"), table_name="mail_intake_runs")
    op.drop_index(op.f("ix_mail_intake_runs_mailbox"), table_name="mail_intake_runs")
    op.drop_table("mail_intake_runs")
