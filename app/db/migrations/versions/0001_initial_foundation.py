"""Initial foundation: extensions and shared DDL helpers

Revision ID: 0001_initial_foundation
Revises:
Create Date: 2026-08-08

Establishes the database primitives every later migration depends on:
`pgcrypto` for `gen_random_uuid()`, `citext` for case-insensitive identifiers,
and a shared `set_updated_at()` trigger function so tables maintain
`updated_at` in the database rather than relying on every writer to do it.

Migrations are hand-written SQL by convention — see the "Data access" section
of README.md.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')

    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    # Extensions are intentionally left in place: dropping them would cascade
    # into any other schema in the database that depends on them.
