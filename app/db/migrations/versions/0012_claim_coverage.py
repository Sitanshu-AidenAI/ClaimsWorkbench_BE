"""Coverage sections, the parties on them, and the excess: CLAWS entry categories 4-7

Revision ID: 0012_claim_coverage
Revises: 0011_claim_casework
Create Date: 2026-08-21

Four tables, written as Alembic operations in the style 0002 set, so
`alembic check` keeps them honest against the mapped models.

These four exist because four of the nine CLAWS entry categories were missing as
*concepts* rather than as fields. `assess_coverage` has always produced a verdict
on whether the policy responds; selecting which sections of it are being paid
under is a different question, and there was nothing to select from. Category 6
depended entirely on category 4, and category 7's deductible had nowhere to
record what had been applied.

Four things in here are load-bearing rather than incidental.

**`claim_coverages.section_key` is unique per claim, and it is derived rather
than generated.** It comes from the peril's own name, so re-proposing against the
same policy lands on the same rows and a handler's position survives it. A
generated id would make every re-proposal a fresh set of sections and silently
discard every decision anybody had taken — which is the failure mode that makes a
"refresh from policy" button unusable.

**`proposed_standpoint` sits beside `standpoint`.** The first is what the rules
concluded, the second is where the section actually stands. Keeping both is what
makes an override legible as an override rather than becoming the truth, which is
the same argument `claim_triage.original_route` makes and the same one the audit
trail's AI-versus-human distinction rests on.

**`claim_parties` is a second parties table, and `fnol_party_id` is not a foreign
key.** `fnol_parties` holds who a broker's email named; this holds who is on the
claim, which is a superset that grows after the notification closes and carries
three roles no email ever mentions — the underwriter, the internal handler and
the loss adjuster, all of which CLAWS category 5 screens. The provenance column
deliberately has no FK and no cascade: a notice can be deleted, and the claim's
record of who was involved has to survive it. That is the same guarantee
`audit_events` gets by carrying no key to the case it describes.

**`claim_deductibles.coverage_id` is nullable and the null case is the common
one.** Most policies carry a single excess at contract level; only some carry one
per section. A null means "the whole claim", which is why it is not defaulted to
a section — and why the read orders nulls first, so the figure a handler looks
for is the first row.

`applied_minor` is what has actually been taken off a payment, not what is due.
It stays zero until a payment carries it, which is why the financials tab has been
reporting the excess as outstanding: there are no payments yet. The arithmetic
that decides what *should* come off a settlement — including the franchise rule,
which does not deduct at all — lives in `app.domain.coverage` and not in a column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_claim_coverage"
down_revision: str | None = "0011_claim_casework"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMPED_TABLES = (
    "claim_coverage_parties",
    "claim_coverages",
    "claim_deductibles",
    "claim_parties",
)


def upgrade() -> None:
    op.create_table(
        "claim_coverages",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("section_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("standpoint", sa.String(length=24), nullable=False),
        sa.Column("proposed_standpoint", sa.String(length=24), nullable=True),
        sa.Column("source_check", sa.String(length=32), nullable=True),
        sa.Column("limit_minor", sa.BigInteger(), nullable=True),
        sa.Column("sublimit_minor", sa.BigInteger(), nullable=True),
        sa.Column("claimed_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("overridden", sa.Boolean(), nullable=False),
        sa.Column("overridden_by", sa.String(length=255), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "limit_minor IS NULL OR limit_minor >= 0",
            name=op.f("ck_claim_coverages_coverage_limit_non_negative"),
        ),
        sa.CheckConstraint(
            "sublimit_minor IS NULL OR sublimit_minor >= 0",
            name=op.f("ck_claim_coverages_coverage_sublimit_non_negative"),
        ),
        sa.CheckConstraint(
            "claimed_minor IS NULL OR claimed_minor >= 0",
            name=op.f("ck_claim_coverages_coverage_claimed_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_coverages_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_coverages")),
        # Named without `op.f` so it reads the same in the model, where the service
        # relies on it to make a retried claim creation idempotent rather than an
        # integrity error.
        sa.UniqueConstraint("claim_id", "section_key", name="uq_claim_coverage_section"),
    )
    op.create_index(
        op.f("ix_claim_coverages_claim_id"), "claim_coverages", ["claim_id"], unique=False
    )
    op.create_index(
        op.f("ix_claim_coverages_standpoint"), "claim_coverages", ["standpoint"], unique=False
    )

    op.create_table(
        "claim_parties",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        # No foreign key, deliberately — see the module docstring.
        sa.Column("fnol_party_id", sa.UUID(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("organisation", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_parties_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_parties")),
    )
    op.create_index(
        op.f("ix_claim_parties_claim_id"), "claim_parties", ["claim_id"], unique=False
    )
    op.create_index(op.f("ix_claim_parties_role"), "claim_parties", ["role"], unique=False)
    op.create_index(
        "ix_claim_parties_claim_role", "claim_parties", ["claim_id", "role"], unique=False
    )

    op.create_table(
        "claim_coverage_parties",
        sa.Column("coverage_id", sa.UUID(), nullable=False),
        sa.Column("party_id", sa.UUID(), nullable=False),
        sa.Column("basis", sa.String(length=160), nullable=True),
        sa.Column("linked_by", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["coverage_id"],
            ["claim_coverages.id"],
            name=op.f("fk_claim_coverage_parties_coverage_id_claim_coverages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["party_id"],
            ["claim_parties.id"],
            name=op.f("fk_claim_coverage_parties_party_id_claim_parties"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_coverage_parties")),
        # The link is the fact; a second identical one is a caller's bug, and the
        # service answers 409 rather than letting this be the thing that catches it.
        sa.UniqueConstraint("coverage_id", "party_id", name="uq_claim_coverage_party"),
    )
    op.create_index(
        op.f("ix_claim_coverage_parties_coverage_id"),
        "claim_coverage_parties",
        ["coverage_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_claim_coverage_parties_party_id"),
        "claim_coverage_parties",
        ["party_id"],
        unique=False,
    )

    op.create_table(
        "claim_deductibles",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("coverage_id", sa.UUID(), nullable=True),
        sa.Column("deductible_type", sa.String(length=24), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("maximum_applied_minor", sa.BigInteger(), nullable=True),
        sa.Column("applied_minor", sa.BigInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("set_by", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name=op.f("ck_claim_deductibles_deductible_amount_non_negative"),
        ),
        sa.CheckConstraint(
            "applied_minor >= 0",
            name=op.f("ck_claim_deductibles_deductible_applied_non_negative"),
        ),
        sa.CheckConstraint(
            "maximum_applied_minor IS NULL OR maximum_applied_minor >= 0",
            name=op.f("ck_claim_deductibles_deductible_maximum_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_deductibles_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["coverage_id"],
            ["claim_coverages.id"],
            name=op.f("fk_claim_deductibles_coverage_id_claim_coverages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_deductibles")),
    )
    op.create_index(
        op.f("ix_claim_deductibles_claim_id"), "claim_deductibles", ["claim_id"], unique=False
    )
    op.create_index(
        "ix_claim_deductibles_claim_coverage",
        "claim_deductibles",
        ["claim_id", "coverage_id"],
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

    op.drop_index("ix_claim_deductibles_claim_coverage", table_name="claim_deductibles")
    op.drop_index(op.f("ix_claim_deductibles_claim_id"), table_name="claim_deductibles")
    op.drop_table("claim_deductibles")

    op.drop_index(
        op.f("ix_claim_coverage_parties_party_id"), table_name="claim_coverage_parties"
    )
    op.drop_index(
        op.f("ix_claim_coverage_parties_coverage_id"), table_name="claim_coverage_parties"
    )
    op.drop_table("claim_coverage_parties")

    op.drop_index("ix_claim_parties_claim_role", table_name="claim_parties")
    op.drop_index(op.f("ix_claim_parties_role"), table_name="claim_parties")
    op.drop_index(op.f("ix_claim_parties_claim_id"), table_name="claim_parties")
    op.drop_table("claim_parties")

    op.drop_index(op.f("ix_claim_coverages_standpoint"), table_name="claim_coverages")
    op.drop_index(op.f("ix_claim_coverages_claim_id"), table_name="claim_coverages")
    op.drop_table("claim_coverages")
