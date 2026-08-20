"""Why an extracted value is what it is, when it was inferred rather than copied

Revision ID: 0010_value_inference_note
Revises: 0009_policy_library
Create Date: 2026-08-20

One nullable column on `extracted_values`, and the reason it is a column rather than
something computed at read time is that it records a *decision made at extraction*.

A date of loss is the field that forced it. A notice states when the loss happened in
the words the broker thought clearest — "13 September 2025, overnight, discovered 14
September 06:20", "overnight on Friday" — and `fnol_cases.date_of_loss` is a
timestamp, so the words have to be resolved into an instant against the date the
notification arrived. That resolution is an inference, and the officer confirming the
claim is entitled to see it stated: which words were read, what they were read as, and
what they were read against.

Recomputing the sentence when the screen is drawn would give a *different* answer once
the notice's own date, the field's text or the resolver changed — which is exactly when
somebody is looking at the value and asking why it says what it says. So it is stored
with the value it explains, cleared with it, and overwritten only by the run that
rewrites the value.

`validation_error` was considered and rejected for this. That column means the value
could not be read and needs a human; this one means the value *was* read, correctly,
and here is how. Putting an explanation in the error column would flag every inferred
date for review and teach the desk to ignore the flag.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_value_inference_note"
down_revision: str | None = "0009_policy_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extracted_values", sa.Column("inference_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("extracted_values", "inference_note")
