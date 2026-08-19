"""Policy identification: the schedule of locations, and the signals to match on

Revision ID: 0007_policy_identification
Revises: 0006_notifications
Create Date: 2026-08-18

Four groups of change, and the reason each is here rather than expressed as JSON
on a row that already exists.

**`policy_locations` is a table.** It is the one place in the reference data where
a JSONB blob genuinely costs something. Property matching has to *search* the
schedule — a loss at warehouse seven of twelve scores nothing against a head
office address — and then *name* the entry that matched, because the sum insured
and the deductible attach to the location rather than to the policy. A candidate
card that prints the policy's headline excess beside a loss at a location with its
own excess is printing the wrong number. `Policy.locations` stays as the raw form
a synchronising integration would land in, and `Policy.primary_location` stays as
a denormalised convenience so nothing that reads it breaks.

**`policies` gains identity and construction columns.** Two email domains rather
than one, because the sender of a commercial notice is usually the broker and the
insured's domain is a different fact about a different party. `prior_policy_id`
because losses are discovered late, and the useful answer to a loss dated before
inception is "here is last year's policy" rather than "outside the period".
Project, contract, principal, contractor and the defects liability period because
on a construction risk those are the identifiers — the party reporting is often a
subcontractor who is insured for their interest and is not the named insured.

**`fnol_cases` gains the notice's side of those signals.** The identification
engine reads case columns and nothing else, so promoting an extracted value to a
column is what makes it a matching signal. That seam is deliberate: it means
"which extracted fields matter for identifying a policy" has a structural answer.
The decision state is here too — status, who confirmed, who referred and why —
because whether the *question* is settled is a different axis from whether a
policy is bound.

**`fnol_policy_matches` gains the working.** `matched_on` held a score per signal
and nothing else, which is enough to rank and not enough to explain. `signals`
holds the whole per-signal result — outcome, weight, both sides' values, the
sentence, and the dataset field the notice's value was read from — stored rather
than recomputed, so that what an officer saw when they bound a policy is
recoverable two years later. `warnings` is separate from `signals` because a
candidate can be certainly the right policy and a poor fit for this loss, and one
list cannot say both. `origin` records how a candidate arrived, which is what lets
a policy an officer found by searching be bound without weakening the rule that a
bound policy is always one of this case's persisted candidates.

Every added column is nullable or defaulted, and the backfill below moves the
existing `Policy.locations` JSON into the new table so nothing that matched
yesterday stops matching today.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_policy_identification"
down_revision: str | None = "0006_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- The schedule of insured locations ----------------------------------
    op.create_table(
        "policy_locations",
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("location_ref", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("postcode", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("sum_insured_minor", sa.BigInteger(), nullable=True),
        sa.Column("deductible_minor", sa.BigInteger(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name="fk_policy_locations_policy_id_policies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_policy_locations"),
    )
    op.create_index("ix_policy_locations_policy_id", "policy_locations", ["policy_id"])
    op.create_index("ix_policy_locations_postcode", "policy_locations", ["postcode"])
    # `set_updated_at()` is created by 0001 and shared by every timestamped table.
    op.execute(
        "CREATE TRIGGER set_policy_locations_updated_at BEFORE UPDATE ON policy_locations"
        " FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # --- Policy: identity and construction ----------------------------------
    policy_columns: list[sa.Column[Any]] = [
        sa.Column("broker_domain", sa.String(length=255), nullable=True),
        sa.Column("insured_domain", sa.String(length=255), nullable=True),
        sa.Column("insurer_name", sa.String(length=255), nullable=True),
        sa.Column("prior_policy_id", sa.UUID(), nullable=True),
        sa.Column("project_name", sa.String(length=255), nullable=True),
        sa.Column("project_reference", sa.String(length=128), nullable=True),
        sa.Column("contract_number", sa.String(length=128), nullable=True),
        sa.Column("principal_name", sa.String(length=255), nullable=True),
        sa.Column("contractor_name", sa.String(length=255), nullable=True),
        sa.Column("site_address", sa.Text(), nullable=True),
        sa.Column("practical_completion_date", sa.Date(), nullable=True),
        sa.Column("maintenance_period_months", sa.Integer(), nullable=True),
    ]
    for column in policy_columns:
        op.add_column("policies", column)

    op.create_foreign_key(
        "fk_policies_prior_policy_id_policies",
        "policies",
        "policies",
        ["prior_policy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_policies_broker_reference", "policies", ["broker_reference"])
    op.create_index("ix_policies_contract_number", "policies", ["contract_number"])
    op.create_index("ix_policies_project_name", "policies", ["project_name"])

    # --- The notice's side of the signals ------------------------------------
    case_columns: list[sa.Column[Any]] = [
        sa.Column("broker_name", sa.String(length=255), nullable=True),
        sa.Column("broker_reference", sa.String(length=128), nullable=True),
        sa.Column("risk_location", sa.Text(), nullable=True),
        sa.Column("loss_postcode", sa.String(length=16), nullable=True),
        sa.Column("project_name", sa.String(length=255), nullable=True),
        sa.Column("contract_number", sa.String(length=128), nullable=True),
        sa.Column("policy_period_stated", sa.String(length=128), nullable=True),
        sa.Column(
            "policy_identification_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_run",
        ),
        sa.Column("policy_identification_ran_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy_referral_reason", sa.Text(), nullable=True),
        sa.Column("policy_referred_by", sa.String(length=255), nullable=True),
        sa.Column("policy_confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("policy_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in case_columns:
        op.add_column("fnol_cases", column)

    op.create_index("ix_fnol_cases_broker_reference", "fnol_cases", ["broker_reference"])
    op.create_index("ix_fnol_cases_loss_postcode", "fnol_cases", ["loss_postcode"])
    op.create_index("ix_fnol_cases_contract_number", "fnol_cases", ["contract_number"])
    op.create_index(
        "ix_fnol_cases_policy_identification_status",
        "fnol_cases",
        ["policy_identification_status"],
    )

    # A case that already carried a confirmed policy is settled, and one that
    # carried candidates has been identified but not decided. Anything else has
    # simply not been through the new engine yet.
    op.execute(
        """
        UPDATE fnol_cases
           SET policy_identification_status = CASE
                 WHEN policy_confirmed THEN 'confirmed'
                 WHEN policy_id IS NOT NULL THEN 'confident_match'
                 WHEN EXISTS (
                        SELECT 1 FROM fnol_policy_matches m
                         WHERE m.fnol_case_id = fnol_cases.id
                 ) THEN 'needs_review'
                 ELSE 'not_run'
               END
        """
    )

    # --- The candidate's working ---------------------------------------------
    candidate_columns: list[sa.Column[Any]] = [
        sa.Column("confidence", sa.String(length=16), nullable=False, server_default="possible"),
        sa.Column(
            "signals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "display",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "period_outcome", sa.String(length=24), nullable=False, server_default="unknown"
        ),
        sa.Column("origin", sa.String(length=24), nullable=False, server_default="engine"),
        sa.Column("recommended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engine_version", sa.String(length=48), nullable=True),
    ]
    for column in candidate_columns:
        op.add_column("fnol_policy_matches", column)

    op.create_index(
        "ix_fnol_policy_matches_case_rank", "fnol_policy_matches", ["fnol_case_id", "rank"]
    )

    # Existing candidates carry the coarse band they were scored with, mapped onto
    # the finer one. They keep no per-signal working — the old scorer produced
    # none — and the next pipeline run replaces them with candidates that do.
    op.execute(
        """
        UPDATE fnol_policy_matches
           SET confidence = CASE match_strength
                              WHEN 'exact' THEN 'exact'
                              WHEN 'high' THEN 'strong'
                              WHEN 'possible' THEN 'possible'
                              ELSE 'weak'
                            END,
               selected_at = CASE WHEN selected THEN updated_at ELSE NULL END
        """
    )

    # The server defaults above exist only to fill the rows that already existed:
    # a NOT NULL column cannot be added to a populated table without one. They are
    # dropped again here so the mapped models stay the single source of truth for
    # what a new row defaults to — which is what keeps `alembic check` honest.
    for table, columns in (
        ("fnol_cases", ("policy_identification_status",)),
        (
            "fnol_policy_matches",
            (
                "confidence",
                "signals",
                "warnings",
                "display",
                "period_outcome",
                "origin",
                "recommended",
            ),
        ),
    ):
        for name in columns:
            op.alter_column(table, name, server_default=None)

    # --- Move the schedule out of JSON --------------------------------------
    # `jsonb_array_elements` over whatever shape the seed wrote. Only `address`
    # is required; a malformed entry is skipped rather than failing the migration,
    # because the JSON column had no schema to have been kept to.
    op.execute(
        """
        INSERT INTO policy_locations (
            id, policy_id, location_ref, description, address, postcode,
            country, sum_insured_minor, deductible_minor, is_primary,
            created_at, updated_at
        )
        SELECT gen_random_uuid(),
               p.id,
               entry->>'location_ref',
               COALESCE(entry->>'description', entry->>'use'),
               COALESCE(entry->>'address', ''),
               entry->>'postcode',
               COALESCE(entry->>'country', p.country),
               NULLIF(entry->>'sum_insured_minor', '')::bigint,
               NULLIF(entry->>'deductible_minor', '')::bigint,
               COALESCE((entry->>'is_primary')::boolean, false),
               now(),
               now()
          FROM policies p
         CROSS JOIN LATERAL jsonb_array_elements(p.locations) AS entry
         WHERE jsonb_typeof(p.locations) = 'array'
           AND COALESCE(entry->>'address', '') <> ''
        """
    )
    # The primary location is part of the schedule too, and matching has to be able
    # to name it. Inserted only where the JSON did not already carry it.
    op.execute(
        """
        INSERT INTO policy_locations (
            id, policy_id, location_ref, description, address, country,
            sum_insured_minor, deductible_minor, is_primary, created_at, updated_at
        )
        SELECT gen_random_uuid(), p.id, NULL, 'Primary insured location',
               p.primary_location, p.country, p.limit_amount_minor,
               p.deductible_amount_minor, true, now(), now()
          FROM policies p
         WHERE p.primary_location IS NOT NULL
           AND p.primary_location <> ''
           AND NOT EXISTS (
                 SELECT 1 FROM policy_locations l
                  WHERE l.policy_id = p.id
                    AND lower(l.address) = lower(p.primary_location)
           )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_fnol_policy_matches_case_rank", table_name="fnol_policy_matches")
    for name in (
        "engine_version",
        "selected_at",
        "recommended",
        "origin",
        "period_outcome",
        "display",
        "warnings",
        "signals",
        "confidence",
    ):
        op.drop_column("fnol_policy_matches", name)

    for name in (
        "ix_fnol_cases_policy_identification_status",
        "ix_fnol_cases_contract_number",
        "ix_fnol_cases_loss_postcode",
        "ix_fnol_cases_broker_reference",
    ):
        op.drop_index(name, table_name="fnol_cases")
    for name in (
        "policy_confirmed_at",
        "policy_confirmed_by",
        "policy_referred_by",
        "policy_referral_reason",
        "policy_identification_ran_at",
        "policy_identification_status",
        "policy_period_stated",
        "contract_number",
        "project_name",
        "loss_postcode",
        "risk_location",
        "broker_reference",
        "broker_name",
    ):
        op.drop_column("fnol_cases", name)

    for name in (
        "ix_policies_project_name",
        "ix_policies_contract_number",
        "ix_policies_broker_reference",
    ):
        op.drop_index(name, table_name="policies")
    op.drop_constraint("fk_policies_prior_policy_id_policies", "policies", type_="foreignkey")
    for name in (
        "maintenance_period_months",
        "practical_completion_date",
        "site_address",
        "contractor_name",
        "principal_name",
        "contract_number",
        "project_reference",
        "project_name",
        "prior_policy_id",
        "insurer_name",
        "insured_domain",
        "broker_domain",
    ):
        op.drop_column("policies", name)

    op.execute("DROP TRIGGER IF EXISTS set_policy_locations_updated_at ON policy_locations")
    op.drop_index("ix_policy_locations_postcode", table_name="policy_locations")
    op.drop_index("ix_policy_locations_policy_id", table_name="policy_locations")
    op.drop_table("policy_locations")
