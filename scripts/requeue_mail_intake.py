"""Clear the attempt count on abandoned mailbox messages so intake collects them again.

A message that fails `CWB_GRAPH_MAX_ATTEMPTS` times stops being retried, and that is
correct: a notice the pipeline genuinely cannot parse must not be re-listed on every
poll forever. But the attempt counter records *that* a message failed, not *why*, and
one of the reasons is "the schema was wrong and has since been fixed". After such a
fix the poll keeps skipping exactly the messages the fix was for, logging
`mail_intake_message_abandoned` and reporting a healthy `failed=0` while collecting
nothing. Nothing retries them, because from the ledger's point of view they have had
their chances.

    # what it would requeue, and the error each one died of
    python scripts/requeue_mail_intake.py

    # only the ones whose recorded error matches
    python scripts/requeue_mail_intake.py --error NotNullViolation

    # do it
    python scripts/requeue_mail_intake.py --error NotNullViolation --commit

Dry-run by default, and `--error` exists so that a fix to one failure mode does not
requeue messages that failed for unrelated reasons — those would burn their attempts
again and land back here with a staler error than before.

Only `attempts` and `status` are reset. `last_error` is deliberately kept: if the
message fails again the next error overwrites it, and if it succeeds the row moves to
`processed` and the old error stops mattering. Wiping it on the way in would remove
the only evidence of why the row needed requeueing, which is the one thing somebody
reading this row later wants to know. `graph_message_id` and the stored envelope are
untouched, so this is a retry of collection and not a re-download.

Messages already `processed` are never touched, whatever `--error` matches. A
processed row has an `fnol_case_id`, and requeueing it would mean a second case for
one email.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import dispose_engine, init_engine, session_scope  # noqa: E402
from app.domain.enums import MailIntakeStatus  # noqa: E402
from app.models.mail_intake import MailIntakeMessage  # noqa: E402


def first_line(text: str | None, width: int = 96) -> str:
    """The error's opening line, which is the part that names the failure."""
    if not text:
        return "(no error recorded)"
    line = text.strip().splitlines()[0]
    return line if len(line) <= width else f"{line[:width]}…"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mailbox",
        help="Only this mailbox. Defaults to every mailbox in the ledger.",
    )
    parser.add_argument(
        "--error",
        help=(
            "Only rows whose recorded error contains this substring, case-insensitively. "
            "Use it to requeue one failure mode at a time."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write the change. Without it the transaction is rolled back.",
    )
    args = parser.parse_args()

    await init_engine()
    try:
        async with session_scope() as session:
            # `!= PROCESSED` rather than `== FAILED`: a row left in `processing` by a
            # worker killed mid-collect is stuck in exactly the same way and needs the
            # same nudge, and naming the one status that must not move is safer than
            # trying to list the ones that may.
            stmt = (
                select(MailIntakeMessage)
                .where(MailIntakeMessage.status != MailIntakeStatus.PROCESSED)
                .where(MailIntakeMessage.attempts > 0)
                .order_by(MailIntakeMessage.received_at)
            )
            if args.mailbox:
                stmt = stmt.where(MailIntakeMessage.mailbox == args.mailbox)

            records = list((await session.execute(stmt)).scalars())

            needle = (args.error or "").lower()
            if needle:
                records = [r for r in records if needle in (r.last_error or "").lower()]

            if not records:
                print("nothing to requeue.")
                return 0

            print(
                f"max_attempts is {settings.graph.max_attempts}; "
                f"{len(records)} row(s) to requeue:\n"
            )
            for record in records:
                print(
                    f"  {record.received_at:%Y-%m-%d %H:%M}  {record.status:<10} "
                    f"attempts={record.attempts}  {(record.subject or '(no subject)')[:52]!r}"
                )
                print(f"      was: {first_line(record.last_error)}")
                record.status = MailIntakeStatus.PENDING
                record.attempts = 0

            if args.commit:
                await session.commit()
                print(f"\ncommitted — the next poll will collect these {len(records)} message(s).")
            else:
                await session.rollback()
                print("\nrolled back — pass --commit to write it.")
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
