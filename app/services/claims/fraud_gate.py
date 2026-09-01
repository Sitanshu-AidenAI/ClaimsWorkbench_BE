"""What the fraud review still owes, for the approval blocks.

One function, and it exists so that three callers cannot answer the question three
ways. The workbench's block list, the approval sheet and the decision endpoint all
have to agree about whether a claim is clear of fraud review, because the first two
draw a screen from it and the third refuses on it — and a screen that says "go
ahead" over an endpoint that says 409 is the specific failure this codebase keeps
having to fix.

**Why this replaced `claims.fraud_flag`.** The block used to read that boolean.
`fraud_flag` is written in exactly one place — triage, from
`TriageCategory.FRAUD_REVIEW` — and nothing in the product ever set it back. A
handler who accepted every red flag on the claim wrote rows to
`claim_fraud_dispositions`, which the block did not read, and was told to "clear the
fraud review flag" by a screen that offered no way to clear it. The claim could not
be approved by anybody, ever. The flag keeps its real job as the machine's signal —
the fraud queue filter and the score — and stops being a gate with no key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain import siu as siu_rules
from app.domain.enums import AnalysisKind, SiuStatus
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository


@dataclass(frozen=True)
class FraudGate:
    """What is still owed on the fraud review, in the two shapes the blocks want."""

    #: Indicators the engine raised that nobody has recorded a verdict against.
    outstanding_indicators: int
    #: An SIU case is running. `not_referred` is not open — it is the resting state
    #: of most claims — and neither closed state is.
    siu_open: bool


async def fraud_gate(
    claim: Any,
    *,
    claims: ClaimRepository,
    cases: FNOLRepository | None = None,
) -> FraudGate:
    """Read the fraud review's outstanding work for one claim.

    Two reads and no writes. Returns no outstanding indicators for a claim with no
    notice behind it, and for a caller that has no notice repository to hand: a claim
    raised by hand has no fraud analysis, so there is nothing to decide about, and
    inventing a block for a claim whose evidence does not exist would be the mirror
    of the bug this replaces. The SIU half is read either way — that record belongs
    to the claim, not to the notice.
    """
    case_id = getattr(claim, "fnol_case_id", None)
    analysis = (
        await cases.get_analysis(case_id, AnalysisKind.FRAUD)
        if cases is not None and case_id
        else None
    )
    result = getattr(analysis, "result", None) or {}
    codes = [str(indicator.get("code", "")) for indicator in result.get("indicators", [])]

    dispositions = list(await claims.list_fraud_dispositions(claim.id))
    disposed = siu_rules.disposed_codes(dispositions)

    siu_case = await claims.get_siu_case(claim.id)
    status = str(siu_case.status) if siu_case else str(SiuStatus.NOT_REFERRED)

    return FraudGate(
        outstanding_indicators=siu_rules.outstanding_indicators(codes, disposed),
        siu_open=(
            siu_case is not None
            and status != str(SiuStatus.NOT_REFERRED)
            and not siu_rules.is_closed(status)
        ),
    )


__all__ = ["FraudGate", "fraud_gate"]
