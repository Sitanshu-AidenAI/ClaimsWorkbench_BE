"""The mailbox can tell us, instead of us asking: Graph change notifications

Revision ID: 0021_mail_subscriptions
Revises: 0020_merge_intake_runs
Create Date: 2026-09-02

One table, holding the live Graph subscription for a mailbox.

Intake has been a poll: every eight seconds, ask the mailbox whether anything
arrived. That works and it is wasteful, and it is the wrong way round — Graph will
call *us* when a message lands, which is both cheaper and faster. This table is
what makes that possible across a restart.

**Why a table at all.** A subscription lives on Graph's side, addressed by an
opaque id. Lose the id and you can neither renew nor delete it, and the next boot
creates a second subscription beside the first — every message then notified
twice, and no way to clean up what you cannot name.

**Why `notification_url` is a column.** Graph posts to the address it was given,
not the address configured now. A development tunnel hands out a new hostname on
every restart, and a subscription pointing at yesterday's tunnel *renews
successfully* while collecting nothing. Keeping the URL is what lets the renewal
sweep tell "needs extending" from "needs replacing" — see
`app.domain.mail_subscription.decide`.

**Why renewal is machinery and not a setting.** Graph caps a subscription on an
Outlook *mail* resource at **4230 minutes** — 70½ hours, a little under three
days. Other resource types allow far more (thirty days for drive items,
twenty-nine for directory objects) and mail does not, so no configuration removes
the need to renew. `expires_at` is indexed because the sweep's only question is
"what expires soon".

The `clientState` secret is deliberately **not** here. It is configuration, it is
the same for every subscription this service creates, and a credential copied into
a table is a credential in one more place than it needs to be.

Polling is not removed and its interval is not changed by this migration.
Microsoft does not guarantee notification delivery — they document that
notifications can be missed — so the poll stays as the reconciliation sweep, and
the `missed` lifecycle event triggers one deliberately.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_mail_subscriptions"
down_revision: str | Sequence[str] | None = "0020_merge_intake_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_subscriptions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("subscription_id", sa.String(length=128), nullable=False),
        sa.Column("mailbox", sa.String(length=320), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("notification_url", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_subscriptions")),
        sa.UniqueConstraint("mailbox", "resource", name="uq_mail_subscription_resource"),
    )
    op.create_index(
        op.f("ix_mail_subscriptions_subscription_id"),
        "mail_subscriptions",
        ["subscription_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_mail_subscriptions_mailbox"), "mail_subscriptions", ["mailbox"], unique=False
    )
    op.create_index(
        op.f("ix_mail_subscriptions_expires_at"),
        "mail_subscriptions",
        ["expires_at"],
        unique=False,
    )
    #: `updated_at` is maintained by the same trigger every other timestamped
    #: table here uses, rather than by the ORM — a renewal that went through raw
    #: SQL would otherwise leave the column behind. The name and the function are
    #: the ones `0013_claim_inspection` established; a table that invented its own
    #: would be the one nobody remembers to drop.
    op.execute(
        "CREATE TRIGGER set_mail_subscriptions_updated_at"
        " BEFORE UPDATE ON mail_subscriptions"
        " FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS set_mail_subscriptions_updated_at ON mail_subscriptions"
    )
    op.drop_index(op.f("ix_mail_subscriptions_expires_at"), table_name="mail_subscriptions")
    op.drop_index(op.f("ix_mail_subscriptions_mailbox"), table_name="mail_subscriptions")
    op.drop_index(op.f("ix_mail_subscriptions_subscription_id"), table_name="mail_subscriptions")
    op.drop_table("mail_subscriptions")
