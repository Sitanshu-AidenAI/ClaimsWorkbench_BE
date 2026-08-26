"""Exposure flags can say "not mentioned" as well as yes and no

Revision ID: 0012_exposure_flags_tri_state
Revises: 0011_value_citations
Create Date: 2026-08-26

`business_interruption`, `structural_damage`, `environmental_exposure` and
`potential_litigation` were `NOT NULL DEFAULT false`, so the schema could hold two
answers to a three-answer question. "The notice says there is no environmental
exposure" and "nobody has looked" both stored `false`.

That is not a modelling nicety. Extraction recovers 22 to 30 of the 30 fields
depending on the model, so *not mentioned* is the ordinary case, not the edge one —
and the deterministic reader made it worse by writing `false` whenever its keyword
was absent, which is most notices. Severity then read the column with `bool()`, an
unassessed pollution exposure scored exactly like an assessed absence, and nothing
anywhere told the officer which of the two they were looking at.

Nullable, and the backfill sets every existing `false` to `NULL`. That looks like
data loss and is the opposite: before this migration `false` meant "no, or nobody
checked, and there is no way to tell", and `NULL` is the honest rendering of that
sentence. `true` rows are untouched — a stated exposure was always unambiguous.

The API keeps coercing these with `bool()`, so the wire contract and the review
screen's checkboxes are unchanged; what changes is that a `NULL` now reaches the
completeness engine as an unanswered required field, which is where an officer
sees it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_exposure_flags_tri_state"
down_revision: str | None = "0011_value_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `(table, column)` for every flag that becomes tri-state.
_FLAGS: tuple[tuple[str, str], ...] = (
    ("fnol_cases", "business_interruption"),
    ("fnol_cases", "structural_damage"),
    ("fnol_cases", "environmental_exposure"),
    ("fnol_cases", "potential_litigation"),
)


def upgrade() -> None:
    for table, column in _FLAGS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Boolean(),
            nullable=True,
            server_default=None,
        )
        # Every stored `false` was written either by a reader that had found nothing
        # or by the column default. Neither is a statement that the exposure is
        # absent, so neither survives as one.
        op.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} IS FALSE")  # noqa: S608


def downgrade() -> None:
    for table, column in _FLAGS:
        op.execute(f"UPDATE {table} SET {column} = FALSE WHERE {column} IS NULL")  # noqa: S608
        op.alter_column(
            table,
            column,
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        )
