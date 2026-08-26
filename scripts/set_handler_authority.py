"""Set a handler's settlement authority, and re-derive the claim flag that depends on it.

A script rather than a migration, deliberately. Rewriting somebody's settlement
authority is a decision about what a person may release, not a schema change — and a
migration would apply it to every environment it ever ran in, including one holding
real money. This runs when somebody means it.

    # what it would do, and why
    python scripts/set_handler_authority.py --claim CLM-2026-000009 --limit 5000000

    # do it
    python scripts/set_handler_authority.py --claim CLM-2026-000009 --limit 5000000 --commit

**The currency is taken from the claim, not chosen here.** A limit is only comparable
with an exposure in the same money — `app.domain.claim_lifecycle.approval_blocks`
refuses rather than guesses when the two differ, because this system holds no
exchange rate. So authorising somebody on a USD claim means giving them a USD limit,
and that is what this does.

It also re-derives `claims.over_authority`, which is a **stored** flag set at triage
against the reserve as it then stood. Raising a limit without refreshing it leaves the
claim blocked by a fact that is no longer true — which is the whole reason this script
exists rather than a hand-written UPDATE.
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

from app.db.session import dispose_engine, init_engine, session_scope  # noqa: E402
from app.models.claim import Claim  # noqa: E402
from app.models.reference_data import Handler  # noqa: E402
from app.repositories.claim import ClaimRepository  # noqa: E402


def money(minor: int | None, currency: str | None) -> str:
    if minor is None:
        return "no authority"
    return f"{minor / 100:,.0f} {currency or '???'}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claim",
        required=True,
        help="The claim whose assigned handler is being authorised. Its currency is used.",
    )
    parser.add_argument(
        "--limit",
        required=True,
        type=int,
        help="The new limit in MAJOR units — 5000000 means five million.",
    )
    parser.add_argument(
        "--also",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Another handler to give the same limit, by address. Repeatable.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write it. Without this the script reports what it would do and rolls back.",
    )
    args = parser.parse_args()

    await init_engine()
    try:
        async with session_scope() as session:
            claims = ClaimRepository(session)

            claim = (
                (await session.execute(select(Claim).where(Claim.reference == args.claim)))
                .scalars()
                .first()
            )
            if claim is None:
                print(f"No claim {args.claim}.")
                return 1

            assignment = await claims.get_assignment(claim.id)
            handler = (
                await session.get(Handler, assignment.handler_id)
                if assignment and assignment.handler_id
                else None
            )
            if handler is None:
                print(
                    f"{claim.reference} has no handler on the file, so there is no authority "
                    "to set. Assign it first."
                )
                return 1

            currency = (claim.currency or "").upper() or "USD"
            limit_minor = args.limit * 100

            print(f"claim   {claim.reference}  reserve {money(claim.reserve_minor, currency)}")
            print(f"        over_authority flag currently {claim.over_authority}")
            held = money(handler.authority_limit_minor, handler.currency)
            print(f"handler {handler.full_name}  {held}  ->  {money(limit_minor, currency)}")

            targets = [handler]
            for email in args.also:
                other = (
                    (
                        await session.execute(
                            select(Handler).where(Handler.email == email.strip().lower())
                        )
                    )
                    .scalars()
                    .first()
                )
                if other is None:
                    print(f"        (no handler at {email} — skipped)")
                    continue
                print(
                    f"handler {other.full_name}  "
                    f"{money(other.authority_limit_minor, other.currency)}"
                    f"  ->  {money(limit_minor, currency)}"
                )
                targets.append(other)

            for target in targets:
                target.authority_limit_minor = limit_minor
                #: The claim's money, not a choice. See the module docstring.
                target.currency = currency

            #: Re-derived the way `ClaimCaseworkService._refresh_authority` does it, so
            #: the flag and the live check cannot disagree about the same claim.
            was = claim.over_authority
            claim.over_authority = (claim.reserve_minor or 0) > limit_minor
            print(f"        over_authority {was} -> {claim.over_authority}")

            if args.commit:
                await session.commit()
                print("\ncommitted.")
            else:
                await session.rollback()
                print("\nrolled back — pass --commit to write it.")
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
