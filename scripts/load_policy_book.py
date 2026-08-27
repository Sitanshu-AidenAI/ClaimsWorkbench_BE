"""Load the synthetic policy book into `policies` + `policy_locations`.

The twelve wordings in `policy/*.pdf` are the *library* — what an officer reads and
what a clause is cited from. Matching runs against the **book**: the flat
`policies` projection in `app/models/reference_data.py`. A deployment whose policy
administration system exports nothing has an empty book, and every notice then
fails to match for a reason that has nothing to do with the notice.

This loads `policy/policy-book.json` into that book so the 24 packs in `case_data/`
have something to match against. It is deliberately outside `app/` — it is demo
data, not application code, and it is idempotent on `policy_number`.

    uv run python scripts/load_policy_book.py
    uv run python scripts/load_policy_book.py --reset   # drop the synthetic rows first
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from app.db.session import dispose_engine, init_engine, session_scope
from app.models.reference_data import Policy, PolicyLocation

BOOK = Path(__file__).resolve().parent.parent / "policy" / "policy-book.json"

#: Every row this script owns carries this prefix on `external_id`, so `--reset`
#: removes exactly what it inserted and never a real policy.
EXTERNAL_ID_PREFIX = "CWB-SYN-"

DATE_FIELDS = ("effective_date", "expiry_date", "practical_completion_date")


def _coerce(payload: dict[str, Any]) -> dict[str, Any]:
    fields = dict(payload)
    for key in DATE_FIELDS:
        if fields.get(key):
            fields[key] = date.fromisoformat(fields[key])
    return fields


async def load(*, reset: bool = False) -> int:
    payloads = json.loads(BOOK.read_text())

    async with session_scope() as session:
        if reset:
            numbers = [p["policy_number"] for p in payloads]
            await session.execute(delete(Policy).where(Policy.policy_number.in_(numbers)))
            await session.flush()

        inserted = 0
        for payload in payloads:
            existing = (
                (
                    await session.execute(
                        select(Policy).where(Policy.policy_number == payload["policy_number"])
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                print(f"  = {payload['policy_number']} already present, left alone")
                continue

            fields = _coerce(payload)
            schedule = fields.pop("locations_scheduled", [])

            policy = Policy(**fields)
            # The raw JSON form a synchronising integration would land in, derived
            # from the schedule rather than written twice — same as app/db/seed.py.
            policy.locations = [
                {
                    "location_ref": ref,
                    "description": desc,
                    "address": address,
                    "postcode": postcode,
                    "sum_insured_minor": sum_insured,
                    "deductible_minor": deductible,
                    "is_primary": is_primary,
                }
                for ref, desc, address, postcode, sum_insured, deductible, is_primary in schedule
            ]
            policy.locations_scheduled = [
                PolicyLocation(
                    location_ref=ref,
                    description=desc,
                    address=address,
                    postcode=postcode,
                    country=payload.get("country"),
                    sum_insured_minor=sum_insured,
                    deductible_minor=deductible,
                    is_primary=is_primary,
                )
                for ref, desc, address, postcode, sum_insured, deductible, is_primary in schedule
            ]
            session.add(policy)
            inserted += 1
            print(f"  + {payload['policy_number']}  {len(schedule)} location(s)")

        await session.flush()
    return inserted


async def _run(*, reset: bool) -> int:
    """Engine setup, load and teardown inside one loop.

    `init_engine` is a coroutine, so it has to be awaited on the same loop the
    session then runs on — calling it from sync code discards it silently and
    `load` fails on a factory that was never built.
    """
    await init_engine()
    try:
        return await load(reset=reset)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true", help="delete the synthetic policies before loading"
    )
    args = parser.parse_args()

    inserted = asyncio.run(_run(reset=args.reset))
    print(f"\n{inserted} policy/policies inserted into the book.")


if __name__ == "__main__":
    main()
