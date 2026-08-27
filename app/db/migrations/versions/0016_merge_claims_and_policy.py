"""Join the claims-workbench and policy-identification revision chains

Revision ID: 0016_merge_claims_and_policy
Revises: 0015_recoveries_and_siu, 0012_exposure_flags_tri_state
Create Date: 2026-08-27

No DDL. This revision exists only to give the graph one head again.

Two feature branches both branched from `0010_value_inference_note` and both added
migrations, so `alembic upgrade head` had two answers and refused to pick one:

    0010_value_inference_note
      ├── 0011_claim_casework → 0012_claim_coverage → 0013_claim_inspection
      │     → 0014_inspection_filing → 0015_recoveries_and_siu
      └── 0011_value_citations → 0012_exposure_flags_tri_state

A merge revision rather than relinking one chain onto the other, and the reason is
which databases stay correct. Relinking — pointing `0011_value_citations` at
`0015_recoveries_and_siu` — reads cleaner and renumbers into one sequence, and it
quietly strands any database sitting on `0012_exposure_flags_tri_state`: that
revision becomes the head of nothing, `upgrade head` finds no work to do, and the
whole claims-workbench chain is never applied there. Nobody gets an error. A merge
revision is right from either side — a database at `0015` applies the citation and
exposure revisions, one at `0012_exposure_flags_tri_state` applies the claims
chain, and both arrive here.

The two chains touch disjoint schema — `claim_*` casework tables on one side,
`extracted_value_citations` and the `fnol_cases` exposure columns on the other — so
there is nothing to reconcile and no ordering between them to get right. That is
what makes this revision empty, and it is worth stating: an empty merge is a claim
about the schema, not a placeholder somebody forgot to fill in.

The duplicated `0011`/`0012` filename prefixes are left as they are. They record
that a branch happened, and Alembic identifies revisions by id rather than by
filename, so renaming would change nothing but the history.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0016_merge_claims_and_policy"
down_revision: str | Sequence[str] | None = (
    "0015_recoveries_and_siu",
    "0012_exposure_flags_tri_state",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do — see the module docstring."""


def downgrade() -> None:
    """Nothing to undo; splitting the graph back in two is the revision's removal."""
