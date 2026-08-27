"""Filing and returning a report: the adjuster's half of the inspection

Revision ID: 0014_inspection_filing
Revises: 0013_claim_inspection
Create Date: 2026-08-24

Two nullable columns on `claim_inspections`, and they exist because 0013 modelled
only one side of a two-sided exchange.

`status = completed` is a **handler** accepting a report. There was nothing to
represent an **adjuster** submitting one, and the two are not the same act by the
same person: between them the report sits with the handler, unread. Without
`filed_at` the adjuster's own queue cannot tell a report still on their desk from
one already gone, which is the single distinction that queue exists to draw — so
the field-inspection board could only ever be fixtures.

**`filed_at` is cleared when the report is sent back.** It answers "is this with
the handler", not "was it ever submitted", and a returned report is owed again.

**`returned_at` is never cleared**, which is the whole reason it is a column
rather than a reading of `status`. "Has this been sent back" has to outlive the
adjuster picking the report up: an inspection returned last week and now back
`in_progress` is still one that was sent back, and the queue's *Sent back* chip
filters on that fact rather than on the state of the visit. Deriving it from
`status = more_needed` would make the chip empty the moment the adjuster resumed
work, which is precisely when a supervisor wants to see it.

No data migration. Every existing inspection has been neither filed nor returned,
which is two nulls.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_inspection_filing"
down_revision: str | None = "0013_claim_inspection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "claim_inspections",
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "claim_inspections",
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("claim_inspections", "returned_at")
    op.drop_column("claim_inspections", "filed_at")
