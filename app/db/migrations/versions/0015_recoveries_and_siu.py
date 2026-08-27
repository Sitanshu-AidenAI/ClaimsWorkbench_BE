"""Recoveries, and the SIU case: the last two unbuilt sections

Revision ID: 0015_recoveries_and_siu
Revises: 0014_inspection_filing
Create Date: 2026-08-26

Five tables, and they close the two sections that had been reporting
`available=False` since the workbench was built. Written in the style 0002 set, so
`alembic check` keeps them honest against the mapped models.

Four things in here are load-bearing rather than incidental.

**`claim_recoveries` holds `expected_minor` and `recovered_minor` as two columns
and never one.** The first is a judgement about the future, the second is a bank
statement. Storing their sum — or netting one into the other — would report a
forecast as an asset, which is the specific way a recovery ledger flatters a loss
ratio. `app.domain.recovery` keeps them apart in the arithmetic for the same
reason, and neither of them touches `claims.reserve_minor`: the reserve is what the
claim is expected to cost, and money coming back is tracked *against* it.

**`claim_recoveries.prospects` is nullable, and the null is not nought.** A
recovery nobody has assessed and one judged hopeless are opposite facts. Storing
0.0 for both would put "no chance" against every subrogation the day it was opened.

**`claim_recovery_tasks.recovery_id` is nullable and the null case is real.**
"Obtain the police report" is recovery work before anybody knows which recovery it
will support, and a task table that demanded a parent would push that into a note.

**`claim_fraud_dispositions` is keyed on the indicator's `code`, not a row id.**
The indicators are derived from the stored fraud analysis and have no rows of their
own, so a verdict has to attach to something stable, and the code is what a rule
keeps across runs. Unique per claim: one verdict per indicator, replaced when
somebody changes their mind, with the previous one kept in the audit trail.

This table is why a disposition now survives a reload. They were held in a working
copy on the client, so a handler who worked through eight indicators lost all eight
by refreshing — and the tab said so, which was honest and useless.

No data migration. Every existing claim has no recoveries and no SIU case, which is
the empty register and `not_referred` respectively — both states the read path
already returns without a row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_recoveries_and_siu"
down_revision: str | None = "0014_inspection_filing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Every one of these carries `updated_at`, so every one needs the trigger the
#: other migrations install. Autogenerate does not know about it — which is the
#: reason this file is hand-written rather than left as it came out.
_TIMESTAMPED_TABLES = (
    "claim_fraud_dispositions",
    "claim_recoveries",
    "claim_recovery_events",
    "claim_recovery_tasks",
    "claim_siu_cases",
)


def upgrade() -> None:
    op.create_table(
        "claim_fraud_dispositions",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_fraud_dispositions_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_fraud_dispositions")),
        sa.UniqueConstraint("claim_id", "code", name="uq_claim_fraud_disposition_code"),
    )
    op.create_index(
        op.f("ix_claim_fraud_dispositions_claim_id"),
        "claim_fraud_dispositions",
        ["claim_id"],
        unique=False,
    )
    op.create_table(
        "claim_recoveries",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("prospects", sa.Numeric(precision=4, scale=3, asdecimal=False), nullable=True),
        sa.Column("expected_minor", sa.BigInteger(), nullable=False),
        sa.Column("recovered_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("party_name", sa.String(length=255), nullable=True),
        sa.Column("party_role", sa.String(length=64), nullable=True),
        sa.Column("party_carrier", sa.String(length=255), nullable=True),
        sa.Column("party_carrier_reference", sa.String(length=128), nullable=True),
        sa.Column("party_contact", sa.String(length=255), nullable=True),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("opened_by", sa.String(length=255), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("limitation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "expected_minor >= 0", name=op.f("ck_claim_recoveries_recovery_expected_non_negative")
        ),
        sa.CheckConstraint(
            "prospects IS NULL OR (prospects >= 0 AND prospects <= 1)",
            name=op.f("ck_claim_recoveries_recovery_prospects_a_probability"),
        ),
        sa.CheckConstraint(
            "recovered_minor >= 0", name=op.f("ck_claim_recoveries_recovery_recovered_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_recoveries_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_recoveries")),
    )
    op.create_index(
        op.f("ix_claim_recoveries_claim_id"), "claim_recoveries", ["claim_id"], unique=False
    )
    op.create_index(op.f("ix_claim_recoveries_kind"), "claim_recoveries", ["kind"], unique=False)
    op.create_index(
        op.f("ix_claim_recoveries_status"), "claim_recoveries", ["status"], unique=False
    )
    op.create_table(
        "claim_siu_cases",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("referred_by", sa.String(length=255), nullable=True),
        sa.Column("referred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("investigator", sa.String(length=255), nullable=True),
        sa.Column("siu_reference", sa.String(length=64), nullable=True),
        sa.Column("referral_reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recommended_actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_siu_cases_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_siu_cases")),
    )
    op.create_index(
        op.f("ix_claim_siu_cases_claim_id"), "claim_siu_cases", ["claim_id"], unique=True
    )
    op.create_index(op.f("ix_claim_siu_cases_status"), "claim_siu_cases", ["status"], unique=False)
    op.create_table(
        "claim_recovery_events",
        sa.Column("recovery_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["recovery_id"],
            ["claim_recoveries.id"],
            name=op.f("fk_claim_recovery_events_recovery_id_claim_recoveries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_recovery_events")),
    )
    op.create_index(
        op.f("ix_claim_recovery_events_recovery_id"),
        "claim_recovery_events",
        ["recovery_id"],
        unique=False,
    )
    op.create_table(
        "claim_recovery_tasks",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("recovery_id", sa.UUID(), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column("done_by", sa.String(length=255), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raised_by", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_recovery_tasks_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recovery_id"],
            ["claim_recoveries.id"],
            name=op.f("fk_claim_recovery_tasks_recovery_id_claim_recoveries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_recovery_tasks")),
    )
    op.create_index(
        op.f("ix_claim_recovery_tasks_claim_id"), "claim_recovery_tasks", ["claim_id"], unique=False
    )
    op.create_index(
        op.f("ix_claim_recovery_tasks_done"), "claim_recovery_tasks", ["done"], unique=False
    )
    op.create_index(
        "ix_claim_recovery_tasks_open", "claim_recovery_tasks", ["claim_id", "done"], unique=False
    )

    for table in _TIMESTAMPED_TABLES:
        op.execute(
            f"CREATE TRIGGER set_{table}_updated_at"
            f" BEFORE UPDATE ON {table}"
            f" FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in _TIMESTAMPED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS set_{table}_updated_at ON {table}")

    op.drop_index("ix_claim_recovery_tasks_open", table_name="claim_recovery_tasks")
    op.drop_index(op.f("ix_claim_recovery_tasks_done"), table_name="claim_recovery_tasks")
    op.drop_index(op.f("ix_claim_recovery_tasks_claim_id"), table_name="claim_recovery_tasks")
    op.drop_table("claim_recovery_tasks")
    op.drop_index(op.f("ix_claim_recovery_events_recovery_id"), table_name="claim_recovery_events")
    op.drop_table("claim_recovery_events")
    op.drop_index(op.f("ix_claim_siu_cases_status"), table_name="claim_siu_cases")
    op.drop_index(op.f("ix_claim_siu_cases_claim_id"), table_name="claim_siu_cases")
    op.drop_table("claim_siu_cases")
    op.drop_index(op.f("ix_claim_recoveries_status"), table_name="claim_recoveries")
    op.drop_index(op.f("ix_claim_recoveries_kind"), table_name="claim_recoveries")
    op.drop_index(op.f("ix_claim_recoveries_claim_id"), table_name="claim_recoveries")
    op.drop_table("claim_recoveries")
    op.drop_index(
        op.f("ix_claim_fraud_dispositions_claim_id"), table_name="claim_fraud_dispositions"
    )
    op.drop_table("claim_fraud_dispositions")
