"""Join the mail-intake-runs chain to the claims chain

Revision ID: 0020_merge_intake_runs
Revises: 0019_no_recovery_reason, 0013_mail_intake_runs
Create Date: 2026-09-02

No DDL. A second merge point, for the same reason as `0016`: two branches both
added migrations off a shared ancestor.

`0013_mail_intake_runs` branched from `0012_exposure_flags_tri_state` — already an
ancestor of the `0016` merge — so bringing it in forked the graph again:

    0012_exposure_flags_tri_state
      ├── 0013_mail_intake_runs                    ← arrived with a later merge
      └── 0016_merge_claims_and_policy → 0017 → 0018 → 0019_no_recovery_reason

**This one was not cosmetic.** `mail_intake_runs` had a model, a repository, a
health service, routes and schemas on the claims branch, and the table itself was
unreachable: the only migration that creates it sat on a head nobody could
`upgrade` to, because `alembic upgrade head` refuses to choose between two. So the
code shipped, the table did not, and every attempt to record a poll failed against
a relation that does not exist. Nothing about that is visible from reading either
branch on its own, which is the argument for merging a fork the day it appears
rather than the day something breaks.

A merge revision rather than relinking, for the reason `0016` gives at length: a
database sitting on either head is carried forward by a merge and silently
stranded by a relink.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0020_merge_intake_runs"
down_revision: str | Sequence[str] | None = (
    "0019_no_recovery_reason",
    "0013_mail_intake_runs",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do — the two chains touch disjoint tables."""


def downgrade() -> None:
    """Nothing to undo; splitting the graph again is this revision's removal."""
