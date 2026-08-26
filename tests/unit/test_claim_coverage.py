"""Coverage sections, the excess, and the service that writes both.

Two halves. The first exercises `app.domain.coverage`, which is pure and takes
plain objects — and the cases worth reading there are the ones where being wrong
costs money: a limit treated as an estimate, a franchise treated as a deduction,
an aggregate netted off a per-claim excess.

The second exercises `ClaimCoverageService` against in-memory repositories that
keep real state, because every assertion is about a *consequence* — the override
was recorded as an override, the proposal was kept beside the position, the
audit event named the section.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import coverage as rules
from app.domain.enums import (
    AuditEventType,
    ClaimPartyRole,
    ClaimStatus,
    CoverageStandpoint,
    DeductibleType,
)
from app.services.claims.coverage import ClaimCoverageService

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def policy(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "perils_covered": ["fire", "flood", "business interruption"],
        "limit_amount_minor": 500_000_00,
        "deductible_amount_minor": 25_000_00,
        "currency": "GBP",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def check(key: str, state: str, detail: str = "") -> dict[str, Any]:
    return {"key": key, "label": key, "state": state, "detail": detail}


def analysis(*checks: dict[str, Any]) -> dict[str, Any]:
    return {"checks": list(checks)}


CLEAN = analysis(
    check("policy", "pass", "Policy located."),
    check("in_force", "pass", "Cover ran across the date of loss."),
    check("peril", "pass", "The notice describes a fire, which the policy names."),
)


def section(
    key: str = "fire",
    *,
    standpoint: str = CoverageStandpoint.CONFIRMED,
    limit_minor: int | None = None,
    claimed_minor: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        section_key=key,
        standpoint=str(standpoint),
        limit_minor=limit_minor,
        claimed_minor=claimed_minor,
    )


# ---------------------------------------------------------------------------
# Proposing sections
# ---------------------------------------------------------------------------


class TestProposeSections:
    def test_one_section_per_named_peril(self) -> None:
        sections = rules.propose_sections(policy(), coverage_result=CLEAN, policy_confirmed=True)
        assert [s.key for s in sections] == ["fire", "flood", "business_interruption"]

    def test_no_policy_proposes_nothing(self) -> None:
        """A claim that matched no policy has no contract to have sections of."""
        assert rules.propose_sections(None, coverage_result=CLEAN, policy_confirmed=False) == []

    def test_a_policy_naming_no_perils_proposes_nothing(self) -> None:
        assert (
            rules.propose_sections(
                policy(perils_covered=[]), coverage_result=CLEAN, policy_confirmed=True
            )
            == []
        )

    def test_the_matching_peril_is_confirmed_and_the_others_are_not(self) -> None:
        """The peril check is one check about the cause, not one per section."""
        sections = {
            s.key: s
            for s in rules.propose_sections(policy(), coverage_result=CLEAN, policy_confirmed=True)
        }
        assert sections["fire"].standpoint is CoverageStandpoint.CONFIRMED
        assert sections["flood"].standpoint is CoverageStandpoint.IN_QUESTION
        assert sections["business_interruption"].standpoint is CoverageStandpoint.IN_QUESTION

    def test_an_unconfirmed_policy_leaves_every_section_in_question(self) -> None:
        """A candidate the matcher ranked highly is not a bound contract."""
        sections = rules.propose_sections(policy(), coverage_result=CLEAN, policy_confirmed=False)
        assert {s.standpoint for s in sections} == {CoverageStandpoint.IN_QUESTION}
        assert all("candidate" in s.note for s in sections)

    def test_a_policy_out_of_force_puts_the_whole_contract_in_question(self) -> None:
        result = analysis(
            check("policy", "pass"),
            check("in_force", "fail", "The loss is dated after cover expired."),
            check("peril", "pass", "fire"),
        )
        sections = rules.propose_sections(policy(), coverage_result=result, policy_confirmed=True)
        assert {s.standpoint for s in sections} == {CoverageStandpoint.IN_QUESTION}
        assert all("whole contract" in s.note for s in sections)

    def test_a_failed_peril_check_never_excludes_a_section_on_its_own(self) -> None:
        """A rule flagging a problem is a reason to look, not a refusal to pay.

        The one case that would make this module a coverage decision engine rather
        than a proposal engine, which is exactly what it must not be.
        """
        result = analysis(
            check("policy", "pass"),
            check("in_force", "pass"),
            check("peril", "fail", "Subsidence is not a peril this policy names."),
        )
        sections = rules.propose_sections(policy(), coverage_result=result, policy_confirmed=True)
        assert CoverageStandpoint.EXCLUDED not in {s.standpoint for s in sections}

    def test_no_analysis_at_all_still_proposes_the_sections(self) -> None:
        """A notice whose pipeline never produced a coverage read still has a policy."""
        sections = rules.propose_sections(policy(), coverage_result=None, policy_confirmed=True)
        assert len(sections) == 3
        assert {s.standpoint for s in sections} == {CoverageStandpoint.IN_QUESTION}

    def test_the_policy_limit_lands_on_every_section(self) -> None:
        sections = rules.propose_sections(policy(), coverage_result=CLEAN, policy_confirmed=True)
        assert {s.limit_minor for s in sections} == {500_000_00}

    def test_section_keys_are_stable_across_reproposals(self) -> None:
        """What lets a handler's override survive a refresh from the policy."""
        first = rules.propose_sections(policy(), coverage_result=CLEAN, policy_confirmed=True)
        second = rules.propose_sections(policy(), coverage_result=CLEAN, policy_confirmed=True)
        assert [s.key for s in first] == [s.key for s in second]


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


class TestExposure:
    def test_only_responding_sections_count(self) -> None:
        sections = [
            section("fire", standpoint=CoverageStandpoint.CONFIRMED, claimed_minor=10_000_00),
            section("flood", standpoint=CoverageStandpoint.EXCLUDED, claimed_minor=99_000_00),
            section("bi", standpoint=CoverageStandpoint.IN_QUESTION, claimed_minor=50_000_00),
        ]
        assert rules.exposure_minor(sections) == 10_000_00

    def test_applies_with_limit_responds(self) -> None:
        sections = [
            section(
                "fire",
                standpoint=CoverageStandpoint.APPLIES_WITH_LIMIT,
                claimed_minor=10_000_00,
            )
        ]
        assert rules.exposure_minor(sections) == 10_000_00

    def test_a_claim_is_capped_at_the_section_limit(self) -> None:
        sections = [section("fire", limit_minor=5_000_00, claimed_minor=12_000_00)]
        assert rules.exposure_minor(sections) == 5_000_00

    def test_a_limit_is_not_an_estimate(self) -> None:
        """A section with a limit and nothing claimed contributes nothing.

        The case that would reserve every claim at policy maximum if it were wrong.
        """
        sections = [section("fire", limit_minor=500_000_00, claimed_minor=None)]
        assert rules.exposure_minor(sections) == 0

    def test_an_unlimited_section_contributes_what_is_claimed(self) -> None:
        sections = [section("fire", limit_minor=None, claimed_minor=12_000_00)]
        assert rules.exposure_minor(sections) == 12_000_00

    def test_over_limit_sections_are_named_rather_than_silently_capped(self) -> None:
        sections = [
            section("fire", limit_minor=5_000_00, claimed_minor=12_000_00),
            section("flood", limit_minor=50_000_00, claimed_minor=1_000_00),
        ]
        assert [s.section_key for s in rules.over_limit(sections)] == ["fire"]

    def test_an_excluded_section_over_its_limit_is_not_flagged(self) -> None:
        """Nothing is being paid under it, so nothing exceeds anything."""
        sections = [
            section(
                "flood",
                standpoint=CoverageStandpoint.EXCLUDED,
                limit_minor=1_00,
                claimed_minor=99_000_00,
            )
        ]
        assert rules.over_limit(sections) == []


# ---------------------------------------------------------------------------
# The excess
# ---------------------------------------------------------------------------


class TestErosion:
    def test_an_aggregate_erodes_across_the_book(self) -> None:
        eroded = rules.erosion(
            deductible_type=DeductibleType.AGGREGATE,
            total_minor=25_000_00,
            applied_here_minor=1_000_00,
            applied_on_other_claims=[4_000_00, 2_000_00],
        )
        assert eroded.applied_elsewhere_minor == 6_000_00
        assert eroded.remaining_minor == 18_000_00
        assert not eroded.exhausted

    def test_a_per_claim_excess_ignores_other_claims(self) -> None:
        """It starts whole every time. Netting other losses off it understates
        what the insured carries on this one."""
        eroded = rules.erosion(
            deductible_type=DeductibleType.PER_CLAIM,
            total_minor=25_000_00,
            applied_here_minor=0,
            applied_on_other_claims=[24_000_00],
        )
        assert eroded.applied_elsewhere_minor == 0
        assert eroded.remaining_minor == 25_000_00

    def test_an_over_applied_aggregate_is_exhausted_not_owed_back(self) -> None:
        eroded = rules.erosion(
            deductible_type=DeductibleType.AGGREGATE,
            total_minor=10_000_00,
            applied_here_minor=8_000_00,
            applied_on_other_claims=[5_000_00],
        )
        assert eroded.remaining_minor == 0
        assert eroded.exhausted


class TestDeduction:
    def test_a_franchise_above_the_threshold_deducts_nothing(self) -> None:
        """The rule that costs money if it is wrong: a franchise is a threshold,
        not a deduction, and treating it as one understates every settlement."""
        assert (
            rules.deduction_for(
                deductible_type=DeductibleType.FRANCHISE,
                excess_minor=10_000_00,
                settlement_minor=50_000_00,
            )
            == 0
        )

    def test_a_franchise_below_the_threshold_pays_nothing(self) -> None:
        assert (
            rules.deduction_for(
                deductible_type=DeductibleType.FRANCHISE,
                excess_minor=10_000_00,
                settlement_minor=4_000_00,
            )
            == 4_000_00
        )

    def test_an_ordinary_excess_deducts(self) -> None:
        assert (
            rules.deduction_for(
                deductible_type=DeductibleType.PER_CLAIM,
                excess_minor=10_000_00,
                settlement_minor=50_000_00,
            )
            == 10_000_00
        )

    def test_an_excess_larger_than_the_loss_takes_the_whole_loss(self) -> None:
        """Not more. The insured does not owe the difference."""
        assert (
            rules.deduction_for(
                deductible_type=DeductibleType.PER_CLAIM,
                excess_minor=10_000_00,
                settlement_minor=3_000_00,
            )
            == 3_000_00
        )

    def test_an_aggregate_deducts_only_what_is_left(self) -> None:
        assert (
            rules.deduction_for(
                deductible_type=DeductibleType.AGGREGATE,
                excess_minor=25_000_00,
                settlement_minor=50_000_00,
                remaining_minor=4_000_00,
            )
            == 4_000_00
        )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class FakeClaimRepo:
    def __init__(self) -> None:
        self.coverages: list[Any] = []
        self.parties: list[Any] = []
        self.links: list[Any] = []
        self.deductibles: list[Any] = []
        self.deleted: list[Any] = []

    def add_coverage(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self.coverages.append(row)
        return row

    async def list_coverages(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return sorted(self.coverages, key=lambda row: row.section_key)

    async def get_coverage(self, claim_id: uuid.UUID, section_key: str) -> Any | None:
        del claim_id
        key = section_key.strip().lower()
        return next((row for row in self.coverages if row.section_key == key), None)

    def add_party(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self.parties.append(row)
        return row

    async def list_parties(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self.parties)

    async def get_party(self, claim_id: uuid.UUID, party_id: Any) -> Any | None:
        del claim_id
        return next((row for row in self.parties if row.id == party_id), None)

    def add_coverage_link(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self.links.append(row)
        return row

    async def get_coverage_link(self, coverage_id: Any, party_id: Any) -> Any | None:
        return next(
            (
                row
                for row in self.links
                if row.coverage_id == coverage_id and row.party_id == party_id
            ),
            None,
        )

    async def delete_coverage_link(self, link: Any) -> None:
        self.links.remove(link)
        self.deleted.append(link)

    def add_deductible(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self.deductibles.append(row)
        return row

    async def list_deductibles(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self.deductibles)

    async def applied_deductibles_on_other_claims(
        self, *, policy_id: Any, exclude_claim_id: Any
    ) -> list[int]:
        del policy_id, exclude_claim_id
        return []


class FakeCaseRepo:
    def __init__(self, parties: list[Any] | None = None, result: Any = None) -> None:
        self._parties = parties or []
        self._result = result

    async def list_parties(self, case_id: Any) -> list[Any]:
        del case_id
        return self._parties

    async def get_analysis(self, case_id: Any, kind: Any) -> Any | None:
        del case_id, kind
        return SimpleNamespace(result=self._result) if self._result is not None else None


class FakePolicyRepo:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    async def get(self, policy_id: Any) -> Any | None:
        del policy_id
        return self._row


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def claim(self, claim: Any, *, event_type: Any, summary: str, actor: str, **rest: Any) -> Any:
        event = {"event_type": str(event_type), "summary": summary, "actor": actor, **rest}
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        return [event["event_type"] for event in self.events]


def make_claim(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "CLM-2026-000001",
        "status": ClaimStatus.IN_REVIEW,
        "currency": "GBP",
        "policy_id": uuid.uuid4(),
        "policy_number": "POL-2026-0198",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def notice_party(role: str, name: str, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "role": role,
        "name": name,
        "organisation": None,
        "email": None,
        "phone": None,
        "address": None,
        "notes": None,
        "is_primary": False,
        "source": "ai",
        "confidence": 0.9,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build(
    *,
    policy_row: Any = None,
    notice_parties: list[Any] | None = None,
    result: Any = None,
) -> tuple[ClaimCoverageService, FakeClaimRepo, RecordingAudit]:
    claims = FakeClaimRepo()
    audit = RecordingAudit()
    service = ClaimCoverageService(
        claims,  # type: ignore[arg-type]
        FakeCaseRepo(notice_parties, result),  # type: ignore[arg-type]
        FakePolicyRepo(policy_row),  # type: ignore[arg-type]
        audit,  # type: ignore[arg-type]
    )
    return service, claims, audit


CASE = SimpleNamespace(id=uuid.uuid4(), reference="FNOL-2026-000003")


class TestMaterialise:
    @pytest.mark.asyncio
    async def test_sections_parties_and_the_excess_all_arrive(self) -> None:
        service, claims, audit = build(
            policy_row=policy(),
            notice_parties=[notice_party("insured", "Northline Logistics")],
            result=CLEAN,
        )
        claim = make_claim()

        await service.materialise(claim, CASE, actor="R. Marsh")

        # Read back through the repository rather than off the insertion list: the
        # order the tab draws is the read's contract, not the order they were added.
        rows = await claims.list_coverages(claim.id)
        assert [row.section_key for row in rows] == [
            "business_interruption",
            "fire",
            "flood",
        ]
        assert [row.name for row in claims.parties] == ["Northline Logistics"]
        assert len(claims.deductibles) == 1
        assert claims.deductibles[0].amount_minor == 25_000_00
        assert audit.types() == [AuditEventType.COVERAGE_SELECTED]

    @pytest.mark.asyncio
    async def test_the_proposal_is_recorded_beside_the_position(self) -> None:
        """What makes a later override legible as an override."""
        service, claims, _ = build(policy_row=policy(), result=CLEAN)
        await service.materialise(make_claim(), CASE, actor="R. Marsh")

        for row in claims.coverages:
            assert row.proposed_standpoint == row.standpoint
            # Falsy rather than `is False`: `overridden` carries a Python-side
            # column default that lands at INSERT, and these rows have not been
            # flushed. The schema coerces with `bool()`, which is the same reading.
            assert not row.overridden
            assert row.confirmed_by is None

    @pytest.mark.asyncio
    async def test_it_is_idempotent(self) -> None:
        """The caller is idempotent on a retried create, so this has to be."""
        service, claims, audit = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()

        await service.materialise(claim, CASE, actor="R. Marsh")
        await service.materialise(claim, CASE, actor="R. Marsh")

        assert len(claims.coverages) == 3
        assert len(audit.events) == 1

    @pytest.mark.asyncio
    async def test_a_claim_with_no_policy_gets_no_sections_and_no_excess(self) -> None:
        service, claims, audit = build(policy_row=None)
        await service.materialise(make_claim(policy_id=None), CASE, actor="R. Marsh")

        assert claims.coverages == []
        assert claims.deductibles == []
        assert audit.events == []

    @pytest.mark.asyncio
    async def test_a_policy_with_no_excess_creates_no_deductible_row(self) -> None:
        service, claims, _ = build(policy_row=policy(deductible_amount_minor=None), result=CLEAN)
        await service.materialise(make_claim(), CASE, actor="R. Marsh")
        assert claims.deductibles == []

    @pytest.mark.asyncio
    async def test_the_assumed_deductible_type_says_it_is_assumed(self) -> None:
        """A stated default a handler can correct, not a silent one."""
        service, claims, _ = build(policy_row=policy(), result=CLEAN)
        await service.materialise(make_claim(), CASE, actor="R. Marsh")

        row = claims.deductibles[0]
        assert row.deductible_type == DeductibleType.PER_CLAIM
        assert "assumed" in (row.comment or "")

    @pytest.mark.asyncio
    async def test_party_provenance_and_source_carry_across(self) -> None:
        service, claims, _ = build(
            policy_row=policy(),
            notice_parties=[
                notice_party("insured", "Northline", source="ai"),
                notice_party("witness", "A. Person", source="human"),
            ],
            result=CLEAN,
        )
        await service.materialise(make_claim(), CASE, actor="R. Marsh")

        assert {row.source for row in claims.parties} == {"ai", "human"}
        assert all(row.fnol_party_id is not None for row in claims.parties)

    @pytest.mark.asyncio
    async def test_an_unmapped_notice_role_lands_on_other(self) -> None:
        service, claims, _ = build(
            policy_row=policy(),
            notice_parties=[notice_party("something_new", "X")],
            result=CLEAN,
        )
        await service.materialise(make_claim(), CASE, actor="R. Marsh")
        assert claims.parties[0].role == ClaimPartyRole.OTHER


class TestSetStandpoint:
    @pytest.mark.asyncio
    async def test_confirming_the_proposal_needs_no_reason(self) -> None:
        service, _, audit = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        audit.events.clear()

        row = await service.set_standpoint(
            claim,
            section_key="fire",
            standpoint=CoverageStandpoint.CONFIRMED,
            actor="R. Marsh",
        )

        assert not row.overridden
        assert row.confirmed_by == "R. Marsh"
        assert audit.types() == [AuditEventType.COVERAGE_SELECTED]

    @pytest.mark.asyncio
    async def test_departing_from_the_proposal_requires_a_reason(self) -> None:
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        with pytest.raises(ValidationError) as raised:
            await service.set_standpoint(
                claim,
                section_key="fire",
                standpoint=CoverageStandpoint.EXCLUDED,
                actor="R. Marsh",
            )
        # The message names both positions, so a handler can see what they are
        # disagreeing with rather than being told a field is missing.
        assert "confirmed" in str(raised.value)
        assert "excluded" in str(raised.value)

    @pytest.mark.asyncio
    async def test_an_override_is_recorded_as_one(self) -> None:
        service, _, audit = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        audit.events.clear()

        row = await service.set_standpoint(
            claim,
            section_key="fire",
            standpoint=CoverageStandpoint.EXCLUDED,
            actor="R. Marsh",
            reason="Arson exclusion applies; police report confirms.",
        )

        assert row.overridden is True
        assert row.overridden_by == "R. Marsh"
        assert row.proposed_standpoint == CoverageStandpoint.CONFIRMED
        assert row.standpoint == CoverageStandpoint.EXCLUDED
        assert audit.types() == [AuditEventType.COVERAGE_OVERRIDDEN]

    @pytest.mark.asyncio
    async def test_absent_fields_are_left_alone(self) -> None:
        """A patch, not a put — the tab sets a figure and a standpoint separately."""
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        await service.set_standpoint(
            claim,
            section_key="fire",
            standpoint=CoverageStandpoint.CONFIRMED,
            actor="R. Marsh",
            claimed_minor=12_000_00,
        )
        row = await service.set_standpoint(
            claim,
            section_key="fire",
            standpoint=CoverageStandpoint.CONFIRMED,
            actor="R. Marsh",
        )
        assert row.claimed_minor == 12_000_00

    @pytest.mark.asyncio
    async def test_an_unknown_section_is_not_created_implicitly(self) -> None:
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        with pytest.raises(NotFoundError):
            await service.set_standpoint(
                claim,
                section_key="cyber",
                standpoint=CoverageStandpoint.CONFIRMED,
                actor="R. Marsh",
            )

    @pytest.mark.asyncio
    async def test_a_decided_claim_refuses(self) -> None:
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        claim.status = ClaimStatus.APPROVED

        with pytest.raises(ConflictError):
            await service.set_standpoint(
                claim,
                section_key="fire",
                standpoint=CoverageStandpoint.EXCLUDED,
                actor="R. Marsh",
                reason="x",
            )


class TestParties:
    @pytest.mark.asyncio
    async def test_a_hand_added_party_is_stamped_human(self) -> None:
        """Never `ai` — everything the extraction found arrived at materialise."""
        service, claims, audit = build()
        party = await service.add_party(
            make_claim(),
            role=ClaimPartyRole.LOSS_ADJUSTER,
            name="I. McGregor",
            actor="R. Marsh",
        )
        assert party.source == "human"
        assert party.confidence is None
        assert party.fnol_party_id is None
        assert audit.types() == [AuditEventType.PARTY_ADDED]
        assert claims.parties == [party]

    @pytest.mark.asyncio
    async def test_a_decided_claim_takes_no_new_parties(self) -> None:
        service, claims, _ = build()
        with pytest.raises(ConflictError):
            await service.add_party(
                make_claim(status=ClaimStatus.REJECTED),
                role=ClaimPartyRole.WITNESS,
                name="X",
                actor="R. Marsh",
            )
        assert claims.parties == []

    @pytest.mark.asyncio
    async def test_linking_a_party_to_a_section(self) -> None:
        service, claims, audit = build(
            policy_row=policy(),
            notice_parties=[notice_party("insured", "Northline")],
            result=CLEAN,
        )
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        party = claims.parties[0]
        audit.events.clear()

        await service.link_party(
            claim,
            section_key="fire",
            party_id=party.id,
            actor="R. Marsh",
            basis="Named insured",
        )

        assert len(claims.links) == 1
        assert audit.types() == [AuditEventType.PARTY_LINKED_TO_COVERAGE]

    @pytest.mark.asyncio
    async def test_a_repeated_link_is_a_conflict(self) -> None:
        service, claims, _ = build(
            policy_row=policy(),
            notice_parties=[notice_party("insured", "Northline")],
            result=CLEAN,
        )
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        party = claims.parties[0]

        await service.link_party(claim, section_key="fire", party_id=party.id, actor="R. Marsh")
        with pytest.raises(ConflictError):
            await service.link_party(claim, section_key="fire", party_id=party.id, actor="R. Marsh")
        assert len(claims.links) == 1

    @pytest.mark.asyncio
    async def test_a_party_from_another_claim_cannot_be_linked(self) -> None:
        """The scoping property: an id from elsewhere misses rather than links."""
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        with pytest.raises(NotFoundError):
            await service.link_party(
                claim, section_key="fire", party_id=uuid.uuid4(), actor="R. Marsh"
            )

    @pytest.mark.asyncio
    async def test_unlinking_removes_the_link_and_keeps_the_party(self) -> None:
        service, claims, _ = build(
            policy_row=policy(),
            notice_parties=[notice_party("insured", "Northline")],
            result=CLEAN,
        )
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        party = claims.parties[0]
        await service.link_party(claim, section_key="fire", party_id=party.id, actor="R. Marsh")

        await service.unlink_party(claim, section_key="fire", party_id=party.id, actor="R. Marsh")

        assert claims.links == []
        assert claims.parties == [party]

    @pytest.mark.asyncio
    async def test_unlinking_something_not_linked_is_a_404(self) -> None:
        service, claims, _ = build(
            policy_row=policy(),
            notice_parties=[notice_party("insured", "Northline")],
            result=CLEAN,
        )
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        with pytest.raises(NotFoundError):
            await service.unlink_party(
                claim,
                section_key="fire",
                party_id=claims.parties[0].id,
                actor="R. Marsh",
            )


class TestSetDeductible:
    @pytest.mark.asyncio
    async def test_setting_the_same_scope_twice_replaces_rather_than_adds(self) -> None:
        service, claims, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        await service.set_deductible(
            claim,
            deductible_type=DeductibleType.AGGREGATE,
            amount_minor=50_000_00,
            actor="R. Marsh",
        )
        await service.set_deductible(
            claim,
            deductible_type=DeductibleType.AGGREGATE,
            amount_minor=60_000_00,
            actor="R. Marsh",
        )

        contract_level = [row for row in claims.deductibles if row.coverage_id is None]
        assert len(contract_level) == 1
        assert contract_level[0].amount_minor == 60_000_00

    @pytest.mark.asyncio
    async def test_a_section_excess_is_a_separate_row(self) -> None:
        service, claims, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        await service.set_deductible(
            claim,
            deductible_type=DeductibleType.PER_OCCURRENCE,
            amount_minor=5_000_00,
            actor="R. Marsh",
            section_key="flood",
        )
        assert len(claims.deductibles) == 2

    @pytest.mark.asyncio
    async def test_the_change_is_audited_with_what_it_was(self) -> None:
        service, _, audit = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        audit.events.clear()

        await service.set_deductible(
            claim,
            deductible_type=DeductibleType.AGGREGATE,
            amount_minor=50_000_00,
            actor="R. Marsh",
        )
        event = audit.events[0]
        assert event["event_type"] == AuditEventType.DEDUCTIBLE_SET
        assert event["before"]["amount_minor"] == 25_000_00
        assert event["after"]["amount_minor"] == 50_000_00

    @pytest.mark.asyncio
    async def test_an_unknown_section_is_refused(self) -> None:
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        with pytest.raises(NotFoundError):
            await service.set_deductible(
                claim,
                deductible_type=DeductibleType.PER_CLAIM,
                amount_minor=1_00,
                actor="R. Marsh",
                section_key="cyber",
            )

    @pytest.mark.asyncio
    async def test_a_decided_claim_refuses(self) -> None:
        service, _, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        claim.status = ClaimStatus.APPROVED

        with pytest.raises(ConflictError):
            await service.set_deductible(
                claim,
                deductible_type=DeductibleType.PER_CLAIM,
                amount_minor=1_00,
                actor="R. Marsh",
            )


class TestErosionRead:
    @pytest.mark.asyncio
    async def test_a_per_claim_excess_does_not_query_other_claims(self) -> None:
        """The query is the expensive part and it cannot change the answer."""
        service, claims, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")

        queried: list[Any] = []

        async def spy(*, policy_id: Any, exclude_claim_id: Any) -> list[int]:
            queried.append(policy_id)
            return [9_999_00]

        claims.applied_deductibles_on_other_claims = spy  # type: ignore[assignment]

        eroded = await service.erosion_for(claim, claims.deductibles[0])
        assert queried == []
        assert eroded.remaining_minor == 25_000_00

    @pytest.mark.asyncio
    async def test_an_aggregate_does_query_them(self) -> None:
        service, claims, _ = build(policy_row=policy(), result=CLEAN)
        claim = make_claim()
        await service.materialise(claim, CASE, actor="R. Marsh")
        await service.set_deductible(
            claim,
            deductible_type=DeductibleType.AGGREGATE,
            amount_minor=25_000_00,
            actor="R. Marsh",
        )

        async def spy(*, policy_id: Any, exclude_claim_id: Any) -> list[int]:
            del policy_id, exclude_claim_id
            return [4_000_00, 2_000_00]

        claims.applied_deductibles_on_other_claims = spy  # type: ignore[assignment]

        row = next(r for r in claims.deductibles if r.coverage_id is None)
        eroded = await service.erosion_for(claim, row)
        assert eroded.applied_elsewhere_minor == 6_000_00
        assert eroded.remaining_minor == 19_000_00


def test_now_is_not_read_by_the_domain_layer() -> None:
    """`app.domain.coverage` takes no clock, and this is what keeps it that way.

    Every function there is a pure map over values; one that reached for the
    current time would make a proposal irreproducible, which is the property the
    module's docstring claims and an auditor relies on.
    """
    import inspect

    source = inspect.getsource(rules)
    assert "datetime.now" not in source
    assert "utcnow" not in source
    # The import is allowed; reading the clock is not.
    assert isinstance(datetime.now(UTC), datetime)
