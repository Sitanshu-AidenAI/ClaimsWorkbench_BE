"""FNOL intake module: cases, documents, intelligence, claims, audit

Revision ID: 0002_fnol_module
Revises: 0001_initial_foundation
Create Date: 2026-08-09

Creates the whole FNOL domain in one revision, because the tables reference each
other and half of it is not a state the application can run in.

Written as Alembic operations rather than raw `op.execute` — the convention this
project sets elsewhere — because the schema here is large enough that the
guarantee worth having is that it matches the mapped models exactly. It does:
`alembic check` reports no drift, which hand-written SQL of this size cannot
promise. Anything Alembic cannot express — the shared `updated_at` triggers — is
spelled out as SQL at the end of `upgrade()`.

`fnol_cases.claim_id` and `claims.fnol_case_id` reference each other, so the
former is created with `use_alter` and its constraint added once both exist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_fnol_module"
down_revision: str | None = "0001_initial_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Every table in this revision that carries `updated_at`. Listed rather than
#: derived, so adding a table without a trigger is a visible omission.
_TIMESTAMPED_TABLES = (
    "cat_events",
    "claim_assignments",
    "claim_triage",
    "claims",
    "fnol_ai_analyses",
    "fnol_cases",
    "fnol_documents",
    "fnol_duplicate_candidates",
    "fnol_exceptions",
    "fnol_extracted_fields",
    "fnol_notes",
    "fnol_parties",
    "fnol_policy_matches",
    "handlers",
    "policies",
    "reference_sequences",
)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("entity_reference", sa.String(length=32), nullable=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_id", "occurred_at"],
        unique=False,
    )
    op.create_index(op.f("ix_audit_events_entity_id"), "audit_events", ["entity_id"], unique=False)
    op.create_index(
        op.f("ix_audit_events_entity_type"), "audit_events", ["entity_type"], unique=False
    )
    op.create_index(
        op.f("ix_audit_events_event_type"), "audit_events", ["event_type"], unique=False
    )
    op.create_index(
        op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"], unique=False
    )
    op.create_table(
        "cat_events",
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("reference", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("perils", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("severity", sa.String(length=24), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("region", sa.String(length=96), nullable=True),
        sa.Column("affected_areas", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6, asdecimal=False), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6, asdecimal=False), nullable=True),
        sa.Column("radius_km", sa.Numeric(precision=8, scale=2, asdecimal=False), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "end_date >= start_date", name=op.f("ck_cat_events_cat_event_window_ordered")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cat_events")),
        sa.UniqueConstraint("external_id", name=op.f("uq_cat_events_external_id")),
    )
    op.create_index(op.f("ix_cat_events_event_type"), "cat_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_cat_events_reference"), "cat_events", ["reference"], unique=True)
    op.create_index(op.f("ix_cat_events_start_date"), "cat_events", ["start_date"], unique=False)
    op.create_table(
        "handlers",
        sa.Column("subject", sa.String(length=128), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("team", sa.String(length=96), nullable=False),
        sa.Column("job_title", sa.String(length=96), nullable=True),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lines_of_business", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("countries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_severity", sa.String(length=16), nullable=False),
        sa.Column("authority_limit_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("open_claims", sa.Integer(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handlers")),
        sa.UniqueConstraint("email", name=op.f("uq_handlers_email")),
        sa.UniqueConstraint("subject", name=op.f("uq_handlers_subject")),
    )
    op.create_index(op.f("ix_handlers_team"), "handlers", ["team"], unique=False)
    op.create_table(
        "policies",
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("policy_number", sa.String(length=64), nullable=False),
        sa.Column("insured_name", sa.String(length=255), nullable=False),
        sa.Column("insured_organisation", sa.String(length=255), nullable=True),
        sa.Column("insured_email", sa.String(length=255), nullable=True),
        sa.Column("insured_phone", sa.String(length=64), nullable=True),
        sa.Column("broker_name", sa.String(length=255), nullable=True),
        sa.Column("broker_reference", sa.String(length=128), nullable=True),
        sa.Column("policy_type", sa.String(length=64), nullable=True),
        sa.Column("line_of_business", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("region", sa.String(length=96), nullable=True),
        sa.Column("primary_location", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6, asdecimal=False), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6, asdecimal=False), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("limit_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("deductible_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("perils_covered", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exclusions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("endorsements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("locations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "expiry_date >= effective_date", name=op.f("ck_policies_policy_period_ordered")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policies")),
        sa.UniqueConstraint("external_id", name=op.f("uq_policies_external_id")),
    )
    op.create_index(op.f("ix_policies_insured_name"), "policies", ["insured_name"], unique=False)
    op.create_index("ix_policies_insured_name_lower", "policies", ["insured_name"], unique=False)
    op.create_index(
        op.f("ix_policies_line_of_business"), "policies", ["line_of_business"], unique=False
    )
    op.create_index(op.f("ix_policies_policy_number"), "policies", ["policy_number"], unique=True)
    op.create_table(
        "reference_sequences",
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("prefix", "year", name=op.f("pk_reference_sequences")),
    )
    op.create_table(
        "fnol_cases",
        sa.Column("reference", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("processing_state", sa.String(length=16), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("message_id", sa.String(length=512), nullable=True),
        sa.Column("thread_id", sa.String(length=512), nullable=True),
        sa.Column("external_reference", sa.String(length=128), nullable=True),
        sa.Column("source_reference", sa.String(length=128), nullable=True),
        sa.Column("source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_body", sa.Text(), nullable=True),
        sa.Column("reporter_name", sa.String(length=255), nullable=True),
        sa.Column("reporter_organisation", sa.String(length=255), nullable=True),
        sa.Column("reporter_role", sa.String(length=96), nullable=True),
        sa.Column("reporter_email", sa.String(length=255), nullable=True),
        sa.Column("reporter_phone", sa.String(length=64), nullable=True),
        sa.Column("policy_number", sa.String(length=64), nullable=True),
        sa.Column("insured_name", sa.String(length=255), nullable=True),
        sa.Column("insured_organisation", sa.String(length=255), nullable=True),
        sa.Column("policy_type", sa.String(length=64), nullable=True),
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("policy_confirmed", sa.Boolean(), nullable=False),
        sa.Column("line_of_business", sa.String(length=48), nullable=True),
        sa.Column("claim_type", sa.String(length=64), nullable=True),
        sa.Column("loss_type", sa.String(length=64), nullable=True),
        sa.Column("complexity", sa.String(length=24), nullable=True),
        sa.Column(
            "classification_confidence",
            sa.Numeric(precision=5, scale=4, asdecimal=False),
            nullable=True,
        ),
        sa.Column("classification_overridden", sa.Boolean(), nullable=False),
        sa.Column("date_of_loss", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loss_location", sa.Text(), nullable=True),
        sa.Column("loss_country", sa.String(length=64), nullable=True),
        sa.Column(
            "loss_latitude", sa.Numeric(precision=9, scale=6, asdecimal=False), nullable=True
        ),
        sa.Column(
            "loss_longitude", sa.Numeric(precision=9, scale=6, asdecimal=False), nullable=True
        ),
        sa.Column("loss_description", sa.Text(), nullable=True),
        sa.Column("cause_of_loss", sa.String(length=255), nullable=True),
        sa.Column("affected_assets", sa.Text(), nullable=True),
        sa.Column("injuries", sa.Integer(), nullable=True),
        sa.Column("fatalities", sa.Integer(), nullable=True),
        sa.Column("business_interruption", sa.Boolean(), nullable=False),
        sa.Column("structural_damage", sa.Boolean(), nullable=False),
        sa.Column("environmental_exposure", sa.Boolean(), nullable=False),
        sa.Column("estimated_loss_minor", sa.BigInteger(), nullable=True),
        sa.Column("repair_estimate_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("police_reference", sa.String(length=128), nullable=True),
        sa.Column("incident_reference", sa.String(length=128), nullable=True),
        sa.Column("authorities_involved", sa.Text(), nullable=True),
        sa.Column("potential_litigation", sa.Boolean(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column(
            "severity_confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True
        ),
        sa.Column("severity_overridden", sa.Boolean(), nullable=False),
        sa.Column("fraud_risk", sa.String(length=16), nullable=True),
        sa.Column("fraud_score", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True),
        sa.Column("coverage_indicator", sa.String(length=32), nullable=True),
        sa.Column(
            "completeness_score", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True
        ),
        sa.Column(
            "extraction_confidence",
            sa.Numeric(precision=5, scale=4, asdecimal=False),
            nullable=True,
        ),
        sa.Column("cat_event_id", sa.UUID(), nullable=True),
        sa.Column(
            "cat_confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True
        ),
        sa.Column("cat_confirmed", sa.Boolean(), nullable=False),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("ai_summary_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("assigned_to", sa.String(length=255), nullable=True),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "estimated_loss_minor IS NULL OR estimated_loss_minor >= 0",
            name=op.f("ck_fnol_cases_estimated_loss_non_negative"),
        ),
        sa.CheckConstraint(
            "fatalities IS NULL OR fatalities >= 0",
            name=op.f("ck_fnol_cases_fatalities_non_negative"),
        ),
        sa.CheckConstraint(
            "injuries IS NULL OR injuries >= 0", name=op.f("ck_fnol_cases_injuries_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["cat_event_id"],
            ["cat_events.id"],
            name=op.f("fk_fnol_cases_cat_event_id_cat_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name=op.f("fk_fnol_cases_policy_id_policies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_cases")),
        sa.UniqueConstraint("claim_id", name=op.f("uq_fnol_cases_claim_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_fnol_cases_idempotency_key")),
        sa.UniqueConstraint("message_id", name=op.f("uq_fnol_cases_message_id")),
    )
    op.create_index(
        op.f("ix_fnol_cases_cat_event_id"), "fnol_cases", ["cat_event_id"], unique=False
    )
    op.create_index(op.f("ix_fnol_cases_channel"), "fnol_cases", ["channel"], unique=False)
    op.create_index(
        op.f("ix_fnol_cases_date_of_loss"), "fnol_cases", ["date_of_loss"], unique=False
    )
    op.create_index(
        op.f("ix_fnol_cases_external_reference"), "fnol_cases", ["external_reference"], unique=False
    )
    op.create_index(op.f("ix_fnol_cases_fraud_risk"), "fnol_cases", ["fraud_risk"], unique=False)
    op.create_index(
        op.f("ix_fnol_cases_insured_name"), "fnol_cases", ["insured_name"], unique=False
    )
    op.create_index(
        op.f("ix_fnol_cases_line_of_business"), "fnol_cases", ["line_of_business"], unique=False
    )
    op.create_index(op.f("ix_fnol_cases_policy_id"), "fnol_cases", ["policy_id"], unique=False)
    op.create_index(
        op.f("ix_fnol_cases_policy_number"), "fnol_cases", ["policy_number"], unique=False
    )
    op.create_index(
        op.f("ix_fnol_cases_processing_state"), "fnol_cases", ["processing_state"], unique=False
    )
    op.create_index(op.f("ix_fnol_cases_received_at"), "fnol_cases", ["received_at"], unique=False)
    op.create_index(op.f("ix_fnol_cases_reference"), "fnol_cases", ["reference"], unique=True)
    op.create_index(op.f("ix_fnol_cases_severity"), "fnol_cases", ["severity"], unique=False)
    op.create_index(op.f("ix_fnol_cases_status"), "fnol_cases", ["status"], unique=False)
    op.create_index(
        "ix_fnol_cases_status_received_at", "fnol_cases", ["status", "received_at"], unique=False
    )
    op.create_index(op.f("ix_fnol_cases_thread_id"), "fnol_cases", ["thread_id"], unique=False)
    op.create_table(
        "claims",
        sa.Column("reference", sa.String(length=32), nullable=False),
        sa.Column("fnol_case_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=True),
        sa.Column("policy_number", sa.String(length=64), nullable=True),
        sa.Column("insured_name", sa.String(length=255), nullable=True),
        sa.Column("claimant_name", sa.String(length=255), nullable=True),
        sa.Column("line_of_business", sa.String(length=48), nullable=True),
        sa.Column("claim_type", sa.String(length=64), nullable=True),
        sa.Column("loss_type", sa.String(length=64), nullable=True),
        sa.Column("loss_description", sa.Text(), nullable=True),
        sa.Column("loss_location", sa.Text(), nullable=True),
        sa.Column("loss_country", sa.String(length=64), nullable=True),
        sa.Column("date_of_loss", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("reserve_minor", sa.BigInteger(), nullable=False),
        sa.Column("paid_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("cat_event_id", sa.UUID(), nullable=True),
        sa.Column("fraud_flag", sa.Boolean(), nullable=False),
        sa.Column("over_authority", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("handler_name", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("paid_minor >= 0", name=op.f("ck_claims_paid_non_negative")),
        sa.CheckConstraint("reserve_minor >= 0", name=op.f("ck_claims_reserve_non_negative")),
        sa.ForeignKeyConstraint(
            ["cat_event_id"],
            ["cat_events.id"],
            name=op.f("fk_claims_cat_event_id_cat_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_claims_fnol_case_id_fnol_cases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name=op.f("fk_claims_policy_id_policies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
    )
    op.create_index(op.f("ix_claims_fnol_case_id"), "claims", ["fnol_case_id"], unique=True)
    op.create_index(
        op.f("ix_claims_line_of_business"), "claims", ["line_of_business"], unique=False
    )
    op.create_index(op.f("ix_claims_policy_number"), "claims", ["policy_number"], unique=False)
    op.create_index(op.f("ix_claims_priority"), "claims", ["priority"], unique=False)
    op.create_index(op.f("ix_claims_reference"), "claims", ["reference"], unique=True)
    op.create_index(op.f("ix_claims_reported_at"), "claims", ["reported_at"], unique=False)
    op.create_index(op.f("ix_claims_severity"), "claims", ["severity"], unique=False)
    op.create_index(op.f("ix_claims_status"), "claims", ["status"], unique=False)
    op.create_index(
        "ix_claims_status_reported_at", "claims", ["status", "reported_at"], unique=False
    )
    op.create_table(
        "fnol_ai_analyses",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=96), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_ai_analyses_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_ai_analyses")),
        sa.UniqueConstraint("fnol_case_id", "kind", name="uq_fnol_analysis_kind"),
    )
    op.create_index(
        op.f("ix_fnol_ai_analyses_fnol_case_id"), "fnol_ai_analyses", ["fnol_case_id"], unique=False
    )
    op.create_table(
        "fnol_documents",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("document_kind", sa.String(length=32), nullable=False),
        sa.Column("extraction_status", sa.String(length=16), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("text_characters", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_documents_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_documents")),
        sa.UniqueConstraint("fnol_case_id", "checksum_sha256", name="uq_fnol_document_checksum"),
    )
    op.create_index(
        op.f("ix_fnol_documents_checksum_sha256"),
        "fnol_documents",
        ["checksum_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_fnol_documents_fnol_case_id"), "fnol_documents", ["fnol_case_id"], unique=False
    )
    op.create_table(
        "fnol_duplicate_candidates",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("candidate_kind", sa.String(length=16), nullable=False),
        sa.Column("candidate_fnol_id", sa.UUID(), nullable=True),
        sa.Column("candidate_claim_id", sa.UUID(), nullable=True),
        sa.Column("candidate_reference", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=False),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(candidate_kind = 'fnol' AND candidate_fnol_id IS NOT NULL) OR (candidate_kind = 'claim' AND candidate_claim_id IS NOT NULL)",
            name=op.f("ck_fnol_duplicate_candidates_duplicate_candidate_target"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_fnol_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_duplicate_candidates_candidate_fnol_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_duplicate_candidates_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_duplicate_candidates")),
        sa.UniqueConstraint(
            "fnol_case_id", "candidate_reference", name="uq_fnol_duplicate_candidate"
        ),
    )
    op.create_index(
        op.f("ix_fnol_duplicate_candidates_fnol_case_id"),
        "fnol_duplicate_candidates",
        ["fnol_case_id"],
        unique=False,
    )
    op.create_table(
        "fnol_exceptions",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_exceptions_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_exceptions")),
        sa.UniqueConstraint("fnol_case_id", "code", name="uq_fnol_exception_code"),
    )
    op.create_index(
        op.f("ix_fnol_exceptions_fnol_case_id"), "fnol_exceptions", ["fnol_case_id"], unique=False
    )
    op.create_index(op.f("ix_fnol_exceptions_status"), "fnol_exceptions", ["status"], unique=False)
    op.create_table(
        "fnol_notes",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_notes_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_notes")),
    )
    op.create_index(
        op.f("ix_fnol_notes_fnol_case_id"), "fnol_notes", ["fnol_case_id"], unique=False
    )
    op.create_table(
        "fnol_parties",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("organisation", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_parties_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_parties")),
    )
    op.create_index(
        op.f("ix_fnol_parties_fnol_case_id"), "fnol_parties", ["fnol_case_id"], unique=False
    )
    op.create_index(op.f("ix_fnol_parties_role"), "fnol_parties", ["role"], unique=False)
    op.create_table(
        "fnol_policy_matches",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("match_strength", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("matched_on", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("selected_by", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_policy_matches_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name=op.f("fk_fnol_policy_matches_policy_id_policies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_policy_matches")),
        sa.UniqueConstraint("fnol_case_id", "policy_id", name="uq_fnol_policy_candidate"),
    )
    op.create_index(
        op.f("ix_fnol_policy_matches_fnol_case_id"),
        "fnol_policy_matches",
        ["fnol_case_id"],
        unique=False,
    )
    op.create_table(
        "claim_assignments",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("handler_id", sa.UUID(), nullable=True),
        sa.Column("handler_name", sa.String(length=255), nullable=True),
        sa.Column("team", sa.String(length=96), nullable=True),
        sa.Column("queue", sa.String(length=96), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("strategy", sa.String(length=24), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("alternatives", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assigned_by", sa.String(length=255), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_assignments_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["handler_id"],
            ["handlers.id"],
            name=op.f("fk_claim_assignments_handler_id_handlers"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_assignments")),
    )
    op.create_index(
        op.f("ix_claim_assignments_claim_id"), "claim_assignments", ["claim_id"], unique=True
    )
    op.create_table(
        "claim_triage",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recommended_route", sa.String(length=96), nullable=False),
        sa.Column("recommended_route_key", sa.String(length=48), nullable=False),
        sa.Column("recommended_priority", sa.String(length=16), nullable=False),
        sa.Column("required_skill", sa.String(length=64), nullable=True),
        sa.Column("required_team", sa.String(length=96), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("overridden", sa.Boolean(), nullable=False),
        sa.Column("overridden_by", sa.String(length=255), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("original_route", sa.String(length=96), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_triage_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_triage")),
    )
    op.create_index(op.f("ix_claim_triage_claim_id"), "claim_triage", ["claim_id"], unique=True)
    op.create_table(
        "fnol_extracted_fields",
        sa.Column("fnol_case_id", sa.UUID(), nullable=False),
        sa.Column("field_path", sa.String(length=96), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4, asdecimal=False), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_document_id", sa.UUID(), nullable=True),
        sa.Column("evidence_snippet", sa.Text(), nullable=True),
        sa.Column("human_modified", sa.Boolean(), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("modified_by", sa.String(length=255), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fnol_case_id"],
            ["fnol_cases.id"],
            name=op.f("fk_fnol_extracted_fields_fnol_case_id_fnol_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["fnol_documents.id"],
            name=op.f("fk_fnol_extracted_fields_source_document_id_fnol_documents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fnol_extracted_fields")),
        sa.UniqueConstraint("fnol_case_id", "field_path", name="uq_fnol_field_path"),
    )
    op.create_index(
        op.f("ix_fnol_extracted_fields_fnol_case_id"),
        "fnol_extracted_fields",
        ["fnol_case_id"],
        unique=False,
    )

    # The back-reference from the notice to the claim it became. Added here
    # rather than inside `create_table` because `claims.fnol_case_id` already
    # points the other way — neither table can carry both constraints at create
    # time.
    op.create_foreign_key(
        "fk_fnol_cases_claim_id_claims",
        "fnol_cases",
        "claims",
        ["claim_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # `updated_at` is maintained by the shared trigger from 0001 rather than by
    # every writer, so a row touched by the ORM, by raw SQL or by a later
    # migration all behave identically. `audit_events` is excluded deliberately:
    # an audit row is never updated.
    for table in _TIMESTAMPED_TABLES:
        op.execute(
            f"CREATE TRIGGER set_{table}_updated_at"
            f" BEFORE UPDATE ON {table}"
            f" FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in _TIMESTAMPED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS set_{table}_updated_at ON {table}")

    op.execute(
        "ALTER TABLE fnol_cases DROP CONSTRAINT IF EXISTS fk_fnol_cases_claim_id_claims"
    )

    op.drop_index(op.f("ix_fnol_extracted_fields_fnol_case_id"), table_name="fnol_extracted_fields")
    op.drop_table("fnol_extracted_fields")
    op.drop_index(op.f("ix_claim_triage_claim_id"), table_name="claim_triage")
    op.drop_table("claim_triage")
    op.drop_index(op.f("ix_claim_assignments_claim_id"), table_name="claim_assignments")
    op.drop_table("claim_assignments")
    op.drop_index(op.f("ix_fnol_policy_matches_fnol_case_id"), table_name="fnol_policy_matches")
    op.drop_table("fnol_policy_matches")
    op.drop_index(op.f("ix_fnol_parties_role"), table_name="fnol_parties")
    op.drop_index(op.f("ix_fnol_parties_fnol_case_id"), table_name="fnol_parties")
    op.drop_table("fnol_parties")
    op.drop_index(op.f("ix_fnol_notes_fnol_case_id"), table_name="fnol_notes")
    op.drop_table("fnol_notes")
    op.drop_index(op.f("ix_fnol_exceptions_status"), table_name="fnol_exceptions")
    op.drop_index(op.f("ix_fnol_exceptions_fnol_case_id"), table_name="fnol_exceptions")
    op.drop_table("fnol_exceptions")
    op.drop_index(
        op.f("ix_fnol_duplicate_candidates_fnol_case_id"), table_name="fnol_duplicate_candidates"
    )
    op.drop_table("fnol_duplicate_candidates")
    op.drop_index(op.f("ix_fnol_documents_fnol_case_id"), table_name="fnol_documents")
    op.drop_index(op.f("ix_fnol_documents_checksum_sha256"), table_name="fnol_documents")
    op.drop_table("fnol_documents")
    op.drop_index(op.f("ix_fnol_ai_analyses_fnol_case_id"), table_name="fnol_ai_analyses")
    op.drop_table("fnol_ai_analyses")
    op.drop_index("ix_claims_status_reported_at", table_name="claims")
    op.drop_index(op.f("ix_claims_status"), table_name="claims")
    op.drop_index(op.f("ix_claims_severity"), table_name="claims")
    op.drop_index(op.f("ix_claims_reported_at"), table_name="claims")
    op.drop_index(op.f("ix_claims_reference"), table_name="claims")
    op.drop_index(op.f("ix_claims_priority"), table_name="claims")
    op.drop_index(op.f("ix_claims_policy_number"), table_name="claims")
    op.drop_index(op.f("ix_claims_line_of_business"), table_name="claims")
    op.drop_index(op.f("ix_claims_fnol_case_id"), table_name="claims")
    op.drop_table("claims")
    op.drop_index(op.f("ix_fnol_cases_thread_id"), table_name="fnol_cases")
    op.drop_index("ix_fnol_cases_status_received_at", table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_status"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_severity"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_reference"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_received_at"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_processing_state"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_policy_number"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_policy_id"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_line_of_business"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_insured_name"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_fraud_risk"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_external_reference"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_date_of_loss"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_channel"), table_name="fnol_cases")
    op.drop_index(op.f("ix_fnol_cases_cat_event_id"), table_name="fnol_cases")
    op.drop_table("fnol_cases")
    op.drop_table("reference_sequences")
    op.drop_index(op.f("ix_policies_policy_number"), table_name="policies")
    op.drop_index(op.f("ix_policies_line_of_business"), table_name="policies")
    op.drop_index("ix_policies_insured_name_lower", table_name="policies")
    op.drop_index(op.f("ix_policies_insured_name"), table_name="policies")
    op.drop_table("policies")
    op.drop_index(op.f("ix_handlers_team"), table_name="handlers")
    op.drop_table("handlers")
    op.drop_index(op.f("ix_cat_events_start_date"), table_name="cat_events")
    op.drop_index(op.f("ix_cat_events_reference"), table_name="cat_events")
    op.drop_index(op.f("ix_cat_events_event_type"), table_name="cat_events")
    op.drop_table("cat_events")
    op.drop_index(op.f("ix_audit_events_occurred_at"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_event_type"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_entity_type"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_entity_id"), table_name="audit_events")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_table("audit_events")
