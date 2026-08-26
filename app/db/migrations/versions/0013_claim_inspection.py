"""The field inspection: the visit, what it found, and what it left owing

Revision ID: 0013_claim_inspection
Revises: 0012_claim_coverage
Create Date: 2026-08-24

Three tables, written as Alembic operations in the style 0002 set, so
`alembic check` keeps them honest against the mapped models.

These exist because the workbench's field-inspection tab was rendering a sentence
explaining its own absence. That was the honest thing to do while there was no
record behind it — but the visit is not a decoration on a large-loss claim, it is
where the number comes from. A handler reserving against a warehouse fire is
reserving against what an adjuster measured on site, and until now the product
could not say who was instructed, whether they had been, or what they found.

Four things in here are load-bearing rather than incidental.

**`claim_inspections.claim_id` is unique, and that is a limitation stated rather
than an oversight.** One inspection per claim. A large loss can carry two
instructions — an adjuster and then a forensic accountant — and this models the
first as a single record whose status can go round the `more_needed →
visit_booked` loop, which is the second *visit* and the common case. Two
concurrent experts are out of scope, and the constraint is what makes that
visible at the point somebody tries, rather than after they have made a mess of
one row.

**`scheduled_at` and `attended_at` are separate columns and must stay separate.**
A booked date that has passed is not attendance. Visits are missed, cancelled on
the morning, and rearranged by the insured, and a schema that inferred arrival
from a date in the past would quietly turn every one of those into a visit that
happened. `app.domain.inspection` only allows attendance to be recorded from
`visit_booked`, for the same reason.

**`claim_inspection_observations.quantified_minor` is nullable, and the null case
is the common one.** An adjuster prices what they can price on site and leaves the
rest to a contractor's quote. A null means *not costed*, **not zero** — and
`ck_claim_inspection_observations_money_pair_complete` insists the currency comes
and goes with it, because a figure with no currency is not an amount.
`app.domain.inspection.quantified_minor` sums only the rows that carry a figure
and reports how many did, so a partial schedule cannot read as a complete one.

**`claim_inspection_actions.owner` is a name and not a foreign key to
`handlers`.** The owner of a follow-up is as often the adjuster, the insured or a
contractor as it is somebody on the desk, and a key to the handler table could not
express three of those four. The partial-shaped index on `(inspection_id, done)`
is what makes the tab's "3 open of 11" badge one indexed read rather than a scan.

There is no data migration and none is needed: every existing claim has no
inspection, which is `not_commissioned` — a state the read path already returns
without a row. See `app.services.claims.sections._inspection`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_claim_inspection"
down_revision: str | None = "0012_claim_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMPED_TABLES = (
    "claim_inspection_actions",
    "claim_inspection_observations",
    "claim_inspections",
)


def upgrade() -> None:
    op.create_table(
        "claim_inspections",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        # The adjuster firm's own reference. Null until they acknowledge — see the
        # model on why one is not generated here.
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column("adjuster_name", sa.String(length=255), nullable=True),
        sa.Column("adjuster_firm", sa.String(length=255), nullable=True),
        sa.Column("commissioned_by", sa.String(length=255), nullable=False),
        sa.Column("commissioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        # Not `scheduled_at` having passed. See the module docstring.
        sa.Column("attended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("site_kind", sa.String(length=64), nullable=True),
        sa.Column("site_address", sa.Text(), nullable=True),
        sa.Column("site_identifier", sa.String(length=128), nullable=True),
        sa.Column("site_contact_name", sa.String(length=255), nullable=True),
        sa.Column("site_contact_phone", sa.String(length=64), nullable=True),
        sa.Column("site_access_note", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("photographs", sa.Integer(), nullable=False),
        sa.Column("measurements", sa.Integer(), nullable=False),
        sa.Column("statements", sa.Integer(), nullable=False),
        sa.Column("documents", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "photographs >= 0", name=op.f("ck_claim_inspections_photographs_non_negative")
        ),
        sa.CheckConstraint(
            "measurements >= 0", name=op.f("ck_claim_inspections_measurements_non_negative")
        ),
        sa.CheckConstraint(
            "statements >= 0", name=op.f("ck_claim_inspections_statements_non_negative")
        ),
        sa.CheckConstraint(
            "documents >= 0", name=op.f("ck_claim_inspections_documents_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_inspections_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_inspections")),
    )
    # Unique rather than merely indexed: one inspection per claim, enforced here.
    op.create_index(
        op.f("ix_claim_inspections_claim_id"), "claim_inspections", ["claim_id"], unique=True
    )
    op.create_index(
        op.f("ix_claim_inspections_status"), "claim_inspections", ["status"], unique=False
    )

    op.create_table(
        "claim_inspection_observations",
        sa.Column("inspection_id", sa.UUID(), nullable=False),
        sa.Column("element", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("finding", sa.Text(), nullable=False),
        # Nullable, and null means "not costed" rather than zero.
        sa.Column("quantified_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("photo_count", sa.Integer(), nullable=False),
        sa.Column("recorded_by", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "quantified_minor IS NULL OR quantified_minor >= 0",
            name=op.f("ck_claim_inspection_observations_quantified_non_negative"),
        ),
        sa.CheckConstraint(
            "photo_count >= 0",
            name=op.f("ck_claim_inspection_observations_photo_count_non_negative"),
        ),
        # The amount and its currency come and go together.
        sa.CheckConstraint(
            "(quantified_minor IS NULL) = (currency IS NULL)",
            name=op.f("ck_claim_inspection_observations_money_pair_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["inspection_id"],
            ["claim_inspections.id"],
            name=op.f("fk_claim_inspection_observations_inspection_id_claim_inspections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_inspection_observations")),
    )
    op.create_index(
        op.f("ix_claim_inspection_observations_inspection_id"),
        "claim_inspection_observations",
        ["inspection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_claim_inspection_observations_severity"),
        "claim_inspection_observations",
        ["severity"],
        unique=False,
    )

    op.create_table(
        "claim_inspection_actions",
        sa.Column("inspection_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        # A name, not a key to `handlers` — see the module docstring.
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
            ["inspection_id"],
            ["claim_inspections.id"],
            name=op.f("fk_claim_inspection_actions_inspection_id_claim_inspections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_inspection_actions")),
    )
    op.create_index(
        op.f("ix_claim_inspection_actions_inspection_id"),
        "claim_inspection_actions",
        ["inspection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_claim_inspection_actions_done"),
        "claim_inspection_actions",
        ["done"],
        unique=False,
    )
    # Named without `op.f` so it reads the same in the model. What the tab's
    # "3 open of 11" badge reads, per inspection, in one index scan.
    op.create_index(
        "ix_claim_inspection_actions_open",
        "claim_inspection_actions",
        ["inspection_id", "done"],
        unique=False,
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

    op.drop_index("ix_claim_inspection_actions_open", table_name="claim_inspection_actions")
    op.drop_index(op.f("ix_claim_inspection_actions_done"), table_name="claim_inspection_actions")
    op.drop_index(
        op.f("ix_claim_inspection_actions_inspection_id"),
        table_name="claim_inspection_actions",
    )
    op.drop_table("claim_inspection_actions")

    op.drop_index(
        op.f("ix_claim_inspection_observations_severity"),
        table_name="claim_inspection_observations",
    )
    op.drop_index(
        op.f("ix_claim_inspection_observations_inspection_id"),
        table_name="claim_inspection_observations",
    )
    op.drop_table("claim_inspection_observations")

    op.drop_index(op.f("ix_claim_inspections_status"), table_name="claim_inspections")
    op.drop_index(op.f("ix_claim_inspections_claim_id"), table_name="claim_inspections")
    op.drop_table("claim_inspections")
