"""Drop the SQL defaults on the citation columns the model defaults in Python

Revision ID: 0017_citation_defaults
Revises: 0016_merge_claims_and_policy
Create Date: 2026-08-27

`0011_value_citations` created `role`, `rank` and `strategy` with SQL `DEFAULT`
clauses. `ExtractedValueCitation` declares the same three with a Python-side
`default=` and no `server_default`, so the model and the schema disagreed and
`alembic check` failed — which is the whole point of running it, and the reason
this is a migration rather than a `server_default=` added to the model.

Dropping the SQL default rather than declaring it on the model, because every
other business column in this schema is defaulted in Python only. Across thirteen
migrations `server_default` appears solely on `id`, `created_at` and `updated_at`,
where the mixins declare it on both sides; `mail_intake_messages.status`,
`claim_recoveries.status` and `claim_siu_cases.status` all take their default from
the ORM and none from Postgres. Pinning three columns of one table the other way
would make this table the exception, and an exception a reviewer has to discover
by reading DDL is worse than a redundant safety net is good.

Nothing depends on the defaults being there. The backfill in `0011_value_citations`
names all three columns explicitly, and every write since goes through the ORM,
which supplies them. The `NOT NULL` constraints are untouched: a raw `INSERT` that
omits these columns fails loudly after this, where before it silently took a value
nobody chose at that call site.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_citation_defaults"
down_revision: str | Sequence[str] | None = "0016_merge_claims_and_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "extracted_value_citations"

#: `(column, type, the default 0011 gave it)` for each column being un-defaulted.
#: The type is repeated because `alter_column` needs `existing_type` to emit the
#: right DDL, and the default is repeated so `downgrade` restores exactly what
#: `0011_value_citations` created rather than an approximation of it.
_DEFAULTED: tuple[tuple[str, sa.types.TypeEngine[object], str], ...] = (
    ("role", sa.String(length=16), "corroborating"),
    ("rank", sa.Integer(), "0"),
    ("strategy", sa.String(length=24), "document-search"),
)


def upgrade() -> None:
    for column, existing_type, _default in _DEFAULTED:
        op.alter_column(
            _TABLE,
            column,
            existing_type=existing_type,
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    for column, existing_type, default in _DEFAULTED:
        op.alter_column(
            _TABLE,
            column,
            existing_type=existing_type,
            existing_nullable=False,
            server_default=default,
        )
