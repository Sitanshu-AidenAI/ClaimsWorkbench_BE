"""An inspection can say which adjuster it belongs to, not just what they are called

Revision ID: 0018_adjuster_identity
Revises: 0017_citation_defaults
Create Date: 2026-08-27

Two nullable columns on `claim_inspections`, and they close a gap between what the
domain says and what the schema could express.

`INSPECTION_WORK_ROLES` puts `loss-adjuster` in the one role set written for them,
with a paragraph explaining why: the report is the adjuster's own work product, and a
system where the handler types it up attributes their findings to somebody else. But
the only link this table had to a person was `adjuster_name` — free text a handler
typed — so there was no way to tell whose visit a row was. Every write that *records*
a finding was therefore gated on `CLAIM_WORK_ROLES`, which excludes adjusters, and
the one route they could reach (`filing`) is refused until attendance and at least one
observation exist. An adjuster could not clear blockers only somebody else could
satisfy. The board said as much on its own face: *"Adjusters are recorded by name
rather than by account, so this board cannot yet be narrowed to your own."*

Widening the guard without these columns would have been worse than leaving it: any
loss adjuster could then record findings on any visit on the desk, because nothing
knew which visits were theirs.

* **`adjuster_email`** is the address the instruction went to. It is knowable at
  commission time and, crucially, knowable *before the adjuster has an account* —
  which is the ordinary case, since a firm is instructed before a person signs in.
* **`adjuster_subject`** is the account, claimed against that email the first time
  the adjuster records anything. Adoption rather than insertion, the same shape
  `HandlerDirectoryService.register` uses, so the link survives the address changing.

Indexed, both of them: the queue's `mine` filter matches on either, and it runs on
every load of the field-inspection board.

No data migration and no backfill. Existing inspections get two nulls, which is the
truthful answer — they were instructed to a name, nobody's account is behind them,
and the handler goes on recording their findings exactly as before. A backfill
guessing an account from `adjuster_name` would be inventing an assignment, and the
first thing it would do is hand somebody write access to a visit on the strength of a
matching string.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_adjuster_identity"
down_revision: str | Sequence[str] | None = "0017_citation_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "claim_inspections",
        sa.Column("adjuster_subject", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "claim_inspections",
        sa.Column("adjuster_email", sa.String(length=320), nullable=True),
    )
    op.create_index(
        op.f("ix_claim_inspections_adjuster_subject"),
        "claim_inspections",
        ["adjuster_subject"],
        unique=False,
    )
    op.create_index(
        op.f("ix_claim_inspections_adjuster_email"),
        "claim_inspections",
        ["adjuster_email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_claim_inspections_adjuster_email"), table_name="claim_inspections")
    op.drop_index(op.f("ix_claim_inspections_adjuster_subject"), table_name="claim_inspections")
    op.drop_column("claim_inspections", "adjuster_email")
    op.drop_column("claim_inspections", "adjuster_subject")
