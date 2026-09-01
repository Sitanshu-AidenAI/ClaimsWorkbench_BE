"""A handler can record that there is nothing to recover

Revision ID: 0019_no_recovery_reason
Revises: 0018_adjuster_identity
Create Date: 2026-08-27

One nullable column on `claims`, and it exists because an empty recovery register
was answering two different questions with the same silence: *nobody has looked at
this yet* and *somebody looked and there is nothing here*.

The section had to assume the first, so every claim on the desk carried "Recovery
has not been considered yet" — including the ones where it had been considered and
closed. `ClaimRecoveriesOut.no_recovery_reason` existed in the payload for exactly
this and was hardcoded `None`, with a comment saying there was nowhere to record
one. This is that nowhere.

On `claims` rather than as a `claim_recoveries` row, because it is the *absence* of
one. A row meaning "no row" would be picked up by the expected total, the recovered
total and the open-pursuit count, and every one of those would then be reporting a
recovery that does not exist.

No `_by` or `_at` columns beside it. The audit event carries the actor and the
timestamp, which is where every other decision on a claim records them, and two
columns that can disagree with the trail are worse than one that cannot.

No backfill. Every existing claim gets null, which is the honest answer: nobody has
recorded a conclusion, and inferring one from an empty register is precisely the
guess this column removes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_no_recovery_reason"
down_revision: str | Sequence[str] | None = "0018_adjuster_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("claims", sa.Column("no_recovery_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claims", "no_recovery_reason")
