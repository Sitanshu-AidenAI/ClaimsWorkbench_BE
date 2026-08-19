"""Role-based access control: the grants an administrator edits

Revision ID: 0008_role_capabilities
Revises: 0007_policy_identification
Create Date: 2026-08-18

Renumbered from 0007 and re-parented. It was written against 0006 at the same time
as `0007_policy_identification` was, on a different branch, so merging the two left
Alembic with two heads and `upgrade head` ambiguous. This one chains after the other
rather than the other way round: that revision is already on the branch this merges
into, so anyone tracking it has applied it, and re-parenting *it* would rewrite
history somebody else has already run.

Nothing here depends on the policy-identification tables — the order is a linear
history rather than a real dependency, which is why re-parenting was enough and no
merge revision was needed.

One table, hand-written as explicit SQL in the style the earlier revisions set, so
the DDL that runs in production is exactly what a reviewer read.

Two decisions in here are load-bearing.

**Absence is a denial.** The primary key is `(role, capability)` and a row means
granted. The alternative — a `granted boolean` column with three states, where a
missing row is "unset" — buys nothing on a grid of checkboxes: "unset" would only
ever render as unchecked, while adding a state every reader of the enforcement path
has to reason about.

**No foreign key on `role`.** Keycloak owns the role list, so there is nothing here
to reference. A grant for a role that has since been deleted in the realm is inert
rather than broken — nobody holds the role, so nobody receives the capability — and
that is a better failure than a delete in the realm being blocked by a row over here.

The table is seeded on boot rather than in this migration, for the reason
`app.main._seed_extraction_schemas` gives for the same choice: the defaults are
configuration rather than schema, and a deployment should come up with the documented
access whether or not anyone remembered to run a data migration.

The seed only ever runs against an **empty** table — see `AccessService.seed`. Seeding
a populated one would restore a grant an administrator had revoked, because a revoked
row does not exist and therefore does not conflict.
"""

from __future__ import annotations

from alembic import op

revision = "0008_role_capabilities"
down_revision = "0007_policy_identification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE role_capabilities (
            role        VARCHAR(64) NOT NULL,
            capability  VARCHAR(64) NOT NULL,
            granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            granted_by  VARCHAR(255),
            CONSTRAINT pk_role_capabilities PRIMARY KEY (role, capability)
        )
        """
    )
    # The read path is always "everything this role may do", so the leading column
    # of the primary key would serve — but the index is declared explicitly because
    # the mapped model declares it, and `alembic check` compares the two.
    op.execute("CREATE INDEX ix_role_capabilities_role ON role_capabilities (role)")


def downgrade() -> None:
    op.execute("DROP TABLE role_capabilities")
