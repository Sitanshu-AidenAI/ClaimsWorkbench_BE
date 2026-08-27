"""Claim casework: notes a handler writes, and the reserve ledger behind the figure

Revision ID: 0011_claim_casework
Revises: 0010_value_inference_note
Create Date: 2026-08-21

Two tables, written as Alembic operations in the style 0002 set, so `alembic check`
keeps them honest against the mapped models.

Three things in here are load-bearing rather than incidental.

**`claim_reserve_movements.amount_minor` is a signed delta, and the check constraint
refuses zero.** A reserve raised from £40,000 to £60,000 is one row of `+2000000`,
and what the claim currently holds is the *sum* of its rows. That is what makes the
ledger the explanation of the figure rather than a log sitting beside it:
`claims.reserve_minor` becomes a cache of this sum, and
`app.domain.claim_lifecycle.held_by_movement_type` is its definition. Storing new
totals instead would make "what changed, and who changed it" a diff between
adjacent rows, which works right up until two movements land in the same second.
Zero is refused because a movement that changes nothing explains nothing, and a
ledger that accepts them teaches its reader to skim.

**The accounting currency pair is validated as a pair.** CLAWS requires every
reserve in both the currency it was incurred in and the currency the book is kept
in, so `accounting_amount_minor` and `accounting_currency` are either both present
or both absent — a converted figure without its currency is not a figure, and a
currency without an amount is not a conversion. Both are nullable because the rate
that relates them is a fact we do not hold yet, and an absent accounting figure is
honest where one converted at 1.0 would be a fabrication that reconciles.

**`claim_notes` is a second notes table, and that is deliberate.** `fnol_notes`
exists and stays. An intake officer's note about a broker's email and a handler's
note about an adjuster's visit are records about different things at different
stages, and one table for both would put the first into a settlement audit. The
`section` column says which workbench tab the note was written on, which is a
property of the note rather than a reason for five more tables.

No `status` column on the movements, deliberately. A reserve movement is a book
entry and it takes effect when it is written; it is a *payment* that waits for
approval, and payments are not this table. A pending reserve would be a figure
that is neither held nor not held.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_claim_casework"
down_revision: str | None = "0010_value_inference_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMPED_TABLES = (
    "claim_notes",
    "claim_reserve_movements",
)


def upgrade() -> None:
    op.create_table(
        "claim_notes",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("section", sa.String(length=24), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_notes_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_notes")),
    )
    op.create_index(op.f("ix_claim_notes_claim_id"), "claim_notes", ["claim_id"], unique=False)
    op.create_index(op.f("ix_claim_notes_section"), "claim_notes", ["section"], unique=False)
    # The one composite the sections read uses: every note on one claim, in order.
    op.create_index(
        "ix_claim_notes_claim_created", "claim_notes", ["claim_id", "created_at"], unique=False
    )

    op.create_table(
        "claim_reserve_movements",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("movement_type", sa.String(length=16), nullable=False),
        sa.Column("sub_movement_type", sa.String(length=64), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("accounting_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("accounting_currency", sa.String(length=3), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("basis", sa.String(length=255), nullable=True),
        sa.Column("set_by", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "amount_minor <> 0",
            name=op.f("ck_claim_reserve_movements_movement_non_zero"),
        ),
        sa.CheckConstraint(
            "(accounting_amount_minor IS NULL) = (accounting_currency IS NULL)",
            name=op.f("ck_claim_reserve_movements_accounting_pair_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_reserve_movements_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_reserve_movements")),
    )
    op.create_index(
        op.f("ix_claim_reserve_movements_claim_id"),
        "claim_reserve_movements",
        ["claim_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_claim_reserve_movements_movement_type"),
        "claim_reserve_movements",
        ["movement_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_claim_reserve_movements_occurred_at"),
        "claim_reserve_movements",
        ["occurred_at"],
        unique=False,
    )
    # Every read of the ledger is "this claim's movements, newest first" — the held
    # figure is a sum over the whole series, so there is no partial read to serve.
    op.create_index(
        "ix_claim_movements_claim_occurred",
        "claim_reserve_movements",
        ["claim_id", "occurred_at"],
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

    op.drop_index("ix_claim_movements_claim_occurred", table_name="claim_reserve_movements")
    op.drop_index(
        op.f("ix_claim_reserve_movements_occurred_at"), table_name="claim_reserve_movements"
    )
    op.drop_index(
        op.f("ix_claim_reserve_movements_movement_type"), table_name="claim_reserve_movements"
    )
    op.drop_index(op.f("ix_claim_reserve_movements_claim_id"), table_name="claim_reserve_movements")
    op.drop_table("claim_reserve_movements")

    op.drop_index("ix_claim_notes_claim_created", table_name="claim_notes")
    op.drop_index(op.f("ix_claim_notes_section"), table_name="claim_notes")
    op.drop_index(op.f("ix_claim_notes_claim_id"), table_name="claim_notes")
    op.drop_table("claim_notes")
