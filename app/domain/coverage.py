"""Coverage sections, and the arithmetic of an excess.

Two jobs, both pure functions over values, for the reason `matching.py` states:
it is arithmetic and rules, it has to be reproducible, and an auditor has to be
able to follow it.

**Proposing sections.** `assessment.assess_coverage` answers "does this policy
respond to this loss at all" and returns one verdict with its per-check working.
That is a different question from "which sections of the contract are we paying
under", which is what CLAWS entry category 4 asks and what a handler actually
argues about. `propose_sections` turns the first into a starting point for the
second: one row per peril the policy names, with a standpoint that reflects what
the pipeline already concluded and a note that says which check drove it.

The output is explicitly a **proposal**. Nothing here decides coverage — every
row lands `IN_QUESTION` unless the policy is bound and its checks passed, and a
handler confirms, narrows or excludes each one. The point of proposing at all is
that a handler who has to create eight rows by hand creates none.

**Eroding an excess.** A per-claim excess starts whole on every claim. An
aggregate one is shared across the policy period, so what is left of it depends
on every other claim on the contract — which is why `erosion` takes the other
claims' applied amounts rather than reading a column.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.enums import (
    ERODING_DEDUCTIBLE_TYPES,
    RESPONDING_STANDPOINTS,
    CoverageStandpoint,
    DeductibleType,
)

# ---------------------------------------------------------------------------
# Proposing sections
# ---------------------------------------------------------------------------

#: Which checks in the coverage read bear on *every* section rather than on one.
#: A policy that was not in force does not respond under any of its sections, so
#: a failure here puts the whole proposal in question rather than one row of it.
_CONTRACT_WIDE_CHECKS = frozenset({"policy", "in_force"})

#: How a check's state reads as a standpoint. `attention` is deliberately not
#: `EXCLUDED`: the pipeline raising a flag is a reason for a handler to look, not
#: a reason to refuse, and a proposal that pre-declined sections would be a
#: coverage decision taken by a rule.
_STANDPOINT_FOR_STATE: dict[str, CoverageStandpoint] = {
    "pass": CoverageStandpoint.CONFIRMED,
    "attention": CoverageStandpoint.IN_QUESTION,
    "fail": CoverageStandpoint.EXCLUDED,
    "unknown": CoverageStandpoint.IN_QUESTION,
}


@dataclass(slots=True, frozen=True)
class ProposedSection:
    """One section of the policy, as a starting position rather than a decision.

    `source_check` is the key of the check in the stored coverage analysis that
    drove the standpoint, or `None` where nothing bore on it. It is carried so the
    claim can say *why* a row arrived at the position it did, and so a handler
    overriding it can see what they are overriding.
    """

    key: str
    label: str
    standpoint: CoverageStandpoint
    limit_minor: int | None
    note: str
    source_check: str | None


def propose_sections(
    policy: Any | None,
    *,
    coverage_result: dict[str, Any] | None,
    policy_confirmed: bool,
) -> list[ProposedSection]:
    """One proposed section per peril the policy names.

    Returns nothing at all when there is no policy. A claim whose notice matched
    nothing has no sections to select, and proposing a generic set would invent a
    contract — the same reason `claims_on_policy` returns nothing rather than
    everything for an unknown policy.

    The perils come from `policies.perils_covered`, which is a flat list of names
    rather than a modelled schedule. That is the honest ceiling on this function:
    it can propose one row per named peril with the policy's *overall* limit
    against each, and it cannot propose a sublimit, because the policy book does
    not carry one. A sublimit is a handler's edit until Global Genius supplies a
    real schedule — `Phase 3.4` — and the note on each row says so.
    """
    if policy is None:
        return []

    perils = [str(peril).strip() for peril in (getattr(policy, "perils_covered", None) or [])]
    perils = [peril for peril in perils if peril]
    if not perils:
        return []

    checks = {
        str(check.get("key")): check
        for check in (coverage_result or {}).get("checks", [])
        if isinstance(check, dict) and check.get("key")
    }
    blocking = _contract_wide_failure(checks)
    limit_minor = getattr(policy, "limit_amount_minor", None)

    sections: list[ProposedSection] = []
    for peril in perils:
        if blocking is not None:
            standpoint = CoverageStandpoint.IN_QUESTION
            note = f"In question for the whole contract: {blocking}"
            source = "in_force"
        elif not policy_confirmed:
            standpoint = CoverageStandpoint.IN_QUESTION
            note = (
                "The policy is a candidate rather than a confirmed match, so no "
                "section can be taken further than in question."
            )
            source = "policy"
        else:
            standpoint, note, source = _peril_standpoint(peril, checks)

        sections.append(
            ProposedSection(
                key=_section_key(peril),
                label=_section_label(peril),
                standpoint=standpoint,
                limit_minor=limit_minor,
                note=note,
                source_check=source,
            )
        )
    return sections


def _contract_wide_failure(checks: dict[str, dict[str, Any]]) -> str | None:
    """The detail of the first contract-wide check that did not pass, if any."""
    for key in ("policy", "in_force"):
        check = checks.get(key)
        if check is None:
            continue
        if str(check.get("state")) in {"fail", "unknown"}:
            return str(check.get("detail") or check.get("label") or key)
    return None


def _peril_standpoint(
    peril: str, checks: dict[str, dict[str, Any]]
) -> tuple[CoverageStandpoint, str, str | None]:
    """What the peril check says about this section, if it says anything.

    The peril check in the stored analysis is one check about the loss's cause, not
    one per section — so it can only confirm the section that matches the cause and
    has nothing to say about the others. Those land `IN_QUESTION` with a note
    saying why, which is honest: nobody has yet decided whether the business
    interruption section responds to a fire, and pretending the machine did is
    exactly what this module refuses to do.
    """
    peril_check = checks.get("peril")
    if peril_check is None:
        return (
            CoverageStandpoint.IN_QUESTION,
            "No peril check ran against this notice.",
            None,
        )

    state = str(peril_check.get("state"))
    detail = str(peril_check.get("detail") or "")
    if state == "pass" and _mentions(detail, peril):
        return (
            _STANDPOINT_FOR_STATE.get(state, CoverageStandpoint.IN_QUESTION),
            f"The cause of loss matches this section. {detail}".strip(),
            "peril",
        )
    if state == "fail":
        return (
            CoverageStandpoint.IN_QUESTION,
            (
                "The cause of loss is not one this policy names, so every section "
                f"needs a decision. {detail}"
            ).strip(),
            "peril",
        )
    return (
        CoverageStandpoint.IN_QUESTION,
        "Not the section the cause of loss points at. Confirm whether it responds.",
        "peril",
    )


def _mentions(detail: str, peril: str) -> bool:
    """Whether the peril check's own words name this section's peril.

    A containment test rather than a parse: the check's detail is prose written for
    a handler, and the alternative is to re-derive the match the check already made.
    """
    return peril.strip().lower() in detail.lower()


def _section_key(peril: str) -> str:
    """A stable identifier for the section, derived from the peril's name.

    Derived rather than generated, so re-proposing on the same policy produces the
    same keys and a handler's override can be matched to the row it overrode.
    """
    return "".join(char if char.isalnum() else "_" for char in peril.strip().lower()).strip("_")


def _section_label(peril: str) -> str:
    cleaned = peril.replace("_", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else peril


# ---------------------------------------------------------------------------
# Exposure across sections
# ---------------------------------------------------------------------------


class Section(Protocol):
    """What the exposure arithmetic needs a coverage row to be."""

    standpoint: str
    limit_minor: int | None
    claimed_minor: int | None


def responding(sections: Iterable[Section]) -> list[Section]:
    """The sections expected to pay something.

    Excluded and in-question sections are both left out, and they are left out for
    different reasons: one will not pay, the other has not been decided. Summing
    the second into an exposure figure would report a maximum as an expectation.
    """
    return [section for section in sections if str(section.standpoint) in RESPONDING_STANDPOINTS]


def exposure_minor(sections: Iterable[Section]) -> int:
    """What the responding sections are expected to cost, capped at their limits.

    Each section contributes the lesser of what is claimed under it and its own
    limit. An unlimited section contributes what is claimed; a section with a limit
    and nothing claimed contributes nothing rather than its limit — a limit is a
    ceiling, not an estimate, and treating it as one is how a book reserves every
    claim at policy maximum.
    """
    total = 0
    for section in responding(sections):
        claimed = int(section.claimed_minor or 0)
        limit = section.limit_minor
        total += claimed if limit is None else min(claimed, int(limit))
    return total


def over_limit(sections: Iterable[Section]) -> list[Section]:
    """Sections where what is claimed exceeds what the section will pay.

    Worth surfacing rather than silently capping: a claim presented above a
    sublimit is a conversation with the insured, and the arithmetic above hides it.
    """
    return [
        section
        for section in responding(sections)
        if section.limit_minor is not None
        and int(section.claimed_minor or 0) > int(section.limit_minor)
    ]


# ---------------------------------------------------------------------------
# The excess
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Erosion:
    """How much of an excess is left, and what ate the rest.

    `applied_elsewhere_minor` is the sum taken on *other* claims, so a handler can
    see that their £25,000 aggregate has £4,000 left because three other losses
    used it — rather than seeing a figure they cannot account for.
    """

    total_minor: int
    applied_here_minor: int
    applied_elsewhere_minor: int

    @property
    def remaining_minor(self) -> int:
        """What is left of the excess. Never negative — an over-applied aggregate
        is exhausted, not owed back."""
        return max(0, self.total_minor - self.applied_here_minor - self.applied_elsewhere_minor)

    @property
    def exhausted(self) -> bool:
        return self.remaining_minor == 0


def erosion(
    *,
    deductible_type: str,
    total_minor: int,
    applied_here_minor: int,
    applied_on_other_claims: Sequence[int] = (),
) -> Erosion:
    """What is left of this excess.

    Only an aggregate erodes across the book. For every other type the other
    claims' figures are **ignored rather than summed**, because a per-claim excess
    starts whole every time — and quietly netting other losses off it would
    understate what the insured carries on this one.
    """
    eroding = deductible_type in ERODING_DEDUCTIBLE_TYPES
    return Erosion(
        total_minor=max(0, int(total_minor)),
        applied_here_minor=max(0, int(applied_here_minor)),
        applied_elsewhere_minor=(
            sum(max(0, int(amount)) for amount in applied_on_other_claims) if eroding else 0
        ),
    )


def deduction_for(
    *,
    deductible_type: str,
    excess_minor: int,
    settlement_minor: int,
    remaining_minor: int | None = None,
) -> int:
    """What comes off a settlement of `settlement_minor`.

    Two rules, and the franchise is the one that catches people out. A **franchise**
    is a threshold: a loss below it pays nothing, and a loss above it pays in full
    with nothing deducted. Treating it as an ordinary excess understates every
    settlement that clears it by the franchise amount.

    Everything else deducts, capped at the settlement — an excess larger than the
    loss means nothing is paid, not that the insured owes the difference. For an
    aggregate, `remaining_minor` is what is left after erosion and is what bites.
    """
    settlement = max(0, int(settlement_minor))
    excess = max(0, int(excess_minor))

    if deductible_type == DeductibleType.FRANCHISE:
        return 0 if settlement >= excess else settlement

    biting = excess if remaining_minor is None else max(0, int(remaining_minor))
    return min(settlement, biting)


__all__ = [
    "Erosion",
    "ProposedSection",
    "Section",
    "deduction_for",
    "erosion",
    "exposure_minor",
    "over_limit",
    "propose_sections",
    "responding",
]
