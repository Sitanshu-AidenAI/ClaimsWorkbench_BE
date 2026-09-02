"""The six workbench sections, tested for what they claim about themselves.

Most of these assertions are about *honesty* rather than about arithmetic, because
that is what this endpoint's design is: three sections are unbuilt and the payload
has to say so rather than look finished. So the tests worth reading are the ones
that would fail if somebody helpfully filled a gap with a plausible default —
`test_the_unbuilt_sections_say_why`, and the two that check an absent excess and an
absent authority limit stay absent instead of arriving as zero.

The arithmetic tests are here too, and they are about one thing: every figure on
the financials tab comes from a row, and the reserve lines agree with the ledger
that produced them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import ActorType, AssignmentStatus, AuditEventType, ClaimStatus
from app.services.claims.sections import ClaimSectionsService

NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeClaimRepo:
    def __init__(
        self,
        *,
        movements: list[Any] | None = None,
        notes: list[Any] | None = None,
        assignment: object | None = None,
        siblings: list[Any] | None = None,
        coverages: list[Any] | None = None,
        parties: list[Any] | None = None,
        links: list[Any] | None = None,
        deductibles: list[Any] | None = None,
        inspection: Any = None,
        observations: list[Any] | None = None,
        inspection_actions: list[Any] | None = None,
        recoveries: list[Any] | None = None,
        recovery_events: list[Any] | None = None,
        recovery_tasks: list[Any] | None = None,
        siu_case: Any = None,
        dispositions: list[Any] | None = None,
    ) -> None:
        self._movements = movements or []
        self._notes = notes or []
        self._assignment = assignment
        self._siblings = siblings or []
        self._coverages = coverages or []
        self._parties = parties or []
        self._links = links or []
        self._deductibles = deductibles or []
        self._inspection = inspection
        self._observations = observations or []
        self._inspection_actions = inspection_actions or []
        self._recoveries = recoveries or []
        self._recovery_events = recovery_events or []
        self._recovery_tasks = recovery_tasks or []
        self._siu_case = siu_case
        self._dispositions = dispositions or []

    async def list_coverages(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return sorted(self._coverages, key=lambda row: row.section_key)

    async def list_parties(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._parties)

    async def list_coverage_links(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._links)

    async def list_deductibles(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._deductibles)

    async def get_inspection(self, claim_id: uuid.UUID) -> Any | None:
        del claim_id
        return self._inspection

    async def list_observations(self, inspection_id: uuid.UUID) -> list[Any]:
        del inspection_id
        return list(self._observations)

    async def list_inspection_actions(self, inspection_id: uuid.UUID) -> list[Any]:
        del inspection_id
        return list(self._inspection_actions)

    async def list_recoveries(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._recoveries)

    async def list_recovery_events(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._recovery_events)

    async def list_recovery_tasks(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._recovery_tasks)

    async def get_siu_case(self, claim_id: uuid.UUID) -> Any | None:
        del claim_id
        return self._siu_case

    async def list_fraud_dispositions(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._dispositions)

    async def list_movements(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return self._movements

    async def list_notes(self, claim_id: uuid.UUID, *, section: str | None = None) -> list[Any]:
        del claim_id
        return [n for n in self._notes if section is None or n.section == section]

    async def get_assignment(self, claim_id: uuid.UUID) -> object | None:
        del claim_id
        return self._assignment

    async def claims_on_policy(
        self, *, policy_id: Any, exclude_claim_id: Any, limit: int = 25
    ) -> list[Any]:
        del policy_id, exclude_claim_id, limit
        return self._siblings


class FakeCaseRepo:
    """The notice behind the claim, and one stored analysis.

    Supplying an analysis implies a case, because the service only reads the
    analysis when there is a notice to read it against — a double that allowed an
    analysis with no case would let a test pass against a state production cannot
    reach.
    """

    def __init__(self, case: object | None = None, analysis: object | None = None) -> None:
        if analysis is not None and case is None:
            case = SimpleNamespace(
                id=uuid.uuid4(), estimated_loss_minor=None, repair_estimate_minor=None
            )
        self._case = case
        self._analysis = analysis

    async def get(self, case_id: Any) -> object | None:
        del case_id
        return self._case

    async def get_analysis(self, case_id: Any, kind: Any) -> object | None:
        del case_id, kind
        return self._analysis


class FakePolicyRepo:
    def __init__(self, policy: object | None = None) -> None:
        self._policy = policy

    async def get(self, policy_id: Any) -> object | None:
        del policy_id
        return self._policy


class FakeHandlerRepo:
    def __init__(self, handler: object | None = None) -> None:
        self._handler = handler

    async def get(self, handler_id: Any) -> object | None:
        del handler_id
        return self._handler


class FakeCoverage:
    """The coverage service, standing in for the one read the assembly makes.

    Only `erosion_for` — the assembly reads repositories directly for everything
    else and asks the service only for the figure that needs a query across other
    claims on the policy.
    """

    async def erosion_for(self, claim: Any, deductible: Any) -> Any:
        del claim
        return SimpleNamespace(
            applied_here_minor=deductible.applied_minor,
            applied_elsewhere_minor=0,
            remaining_minor=max(0, deductible.amount_minor - deductible.applied_minor),
        )


class FakeAudit:
    def __init__(self, events: list[Any] | None = None) -> None:
        self._events = events or []

    async def history(self, entity_id: Any, *, related_ids: tuple[Any, ...] = ()) -> list[Any]:
        del entity_id, related_ids
        return self._events


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_claim(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "CLM-000001",
        "fnol_case_id": uuid.uuid4(),
        "policy_id": uuid.uuid4(),
        "status": ClaimStatus.IN_REVIEW,
        "severity": "high",
        "currency": "GBP",
        "reserve_minor": 0,
        "paid_minor": 0,
        "fraud_flag": False,
        "over_authority": False,
        #: The handler's conclusion that there is nothing to recover. Null on a claim
        #: nobody has reached one on — which is where the recoveries section's three
        #: empty states are told apart.
        "no_recovery_reason": None,
        "closed_at": None,
        "handler_name": "R. Marsh",
        "loss_type": "fire",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def movement(head: str, amount_minor: int, *, at: datetime, rationale: str = "x") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        movement_type=head,
        sub_movement_type=None,
        amount_minor=amount_minor,
        currency="GBP",
        rationale=rationale,
        basis=None,
        set_by="R. Marsh",
        occurred_at=at,
    )


def note(section: str, body: str, *, at: datetime) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), author="R. Marsh", body=body, section=section, created_at=at
    )


def coverage(
    key: str = "fire",
    *,
    standpoint: str = "confirmed",
    proposed: str | None = "confirmed",
    limit_minor: int | None = None,
    claimed_minor: int | None = None,
    overridden: bool = False,
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        section_key=key,
        label=key.replace("_", " ").title(),
        standpoint=standpoint,
        proposed_standpoint=proposed,
        source_check="peril",
        limit_minor=limit_minor,
        sublimit_minor=None,
        claimed_minor=claimed_minor,
        currency="GBP",
        note="Proposed from the policy's named perils.",
        overridden=overridden,
        overridden_by="R. Marsh" if overridden else None,
        override_reason="Arson exclusion." if overridden else None,
        confirmed_by=None,
        confirmed_at=None,
    )


def party(name: str = "Northline Logistics", *, role: str = "insured") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        name=name,
        organisation=None,
        email=None,
        phone=None,
        address=None,
        notes=None,
        is_primary=role == "insured",
        source="ai",
        confidence=0.9,
        fnol_party_id=uuid.uuid4(),
    )


def link(coverage_row: Any, party_row: Any, *, basis: str | None = None) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        coverage_id=coverage_row.id,
        party_id=party_row.id,
        basis=basis,
        linked_by="R. Marsh",
    )


def deductible(
    *,
    coverage_row: Any = None,
    amount_minor: int = 25_000_00,
    applied_minor: int = 0,
    deductible_type: str = "per_claim",
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        coverage_id=coverage_row.id if coverage_row is not None else None,
        deductible_type=deductible_type,
        amount_minor=amount_minor,
        currency="GBP",
        maximum_applied_minor=None,
        applied_minor=applied_minor,
        comment=None,
        set_by="R. Marsh",
    )


def event(event_type: str, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "event_type": event_type,
        "summary": "Something happened.",
        "actor": "R. Marsh",
        "actor_type": ActorType.HUMAN,
        "occurred_at": NOW,
        "before": None,
        "after": None,
        "entity_reference": "CLM-000001",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build(
    *,
    claims: FakeClaimRepo | None = None,
    cases: FakeCaseRepo | None = None,
    policies: FakePolicyRepo | None = None,
    handlers: FakeHandlerRepo | None = None,
    audit: FakeAudit | None = None,
) -> ClaimSectionsService:
    return ClaimSectionsService(
        claims or FakeClaimRepo(),  # type: ignore[arg-type]
        cases or FakeCaseRepo(),  # type: ignore[arg-type]
        policies or FakePolicyRepo(),  # type: ignore[arg-type]
        handlers or FakeHandlerRepo(),  # type: ignore[arg-type]
        audit or FakeAudit(),  # type: ignore[arg-type]
        FakeCoverage(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


class TestHonesty:
    @pytest.mark.asyncio
    async def test_no_section_reports_itself_unbuilt_any_more(self) -> None:
        """All six now hold records. This is what would fail if one regressed.

        The recovery register was the last, and the field inspection the one before
        it. What replaced `available=False` in both cases is not a default: an empty
        register and an unreferred claim are *states*, which is why they are
        reported as available with nothing in them.
        """
        sections = await build().build(make_claim())

        for section in (
            sections.inspection,
            sections.assessment,
            sections.financials,
            sections.fraud,
            sections.recoveries,
        ):
            assert section.available is True

        #: The assessment is the one that still carries a reason while available —
        #: the sections are real and the priced damage breakdown is not.
        assert sections.recoveries.unavailable_reason is None
        assert sections.fraud.unavailable_reason is None

    @pytest.mark.asyncio
    async def test_the_real_sections_do_not_claim_to_be_unbuilt(self) -> None:
        sections = await build().build(make_claim())
        assert sections.financials.available is True
        assert sections.financials.unavailable_reason is None

    @pytest.mark.asyncio
    async def test_the_assessment_is_available_but_still_names_what_it_lacks(self) -> None:
        """It became real when coverage did, and it is still not complete.

        `available` is true because the sections, the parties and the excess are
        records now. `unavailable_reason` still carries a sentence, because the
        damage is not priced head by head — and naming *that* rather than the whole
        tab is the distinction the field exists to draw.
        """
        assessment = (await build().build(make_claim())).assessment
        assert assessment.available is True
        assert assessment.unavailable_reason
        assert "priced" in assessment.unavailable_reason
        assert assessment.damage_heads == []

    @pytest.mark.asyncio
    async def test_an_uncommissioned_inspection_invents_no_adjuster(self) -> None:
        """No visit is a state this system holds, not a gap in it.

        So `available` is true and `unavailable_reason` is null — and every field
        that would describe a visit is absent rather than defaulted, because there
        is no visit to describe. The site is null in particular: the claim knows
        where the loss happened, but nobody has been sent there.
        """
        inspection = (await build().build(make_claim())).inspection
        assert inspection.available is True
        assert inspection.unavailable_reason is None
        assert inspection.status == "not_commissioned"
        assert inspection.adjuster_name is None
        assert inspection.site is None
        assert inspection.observations == []
        assert inspection.evidence.photographs == 0

    @pytest.mark.asyncio
    async def test_an_uncommissioned_inspection_offers_the_one_move_it_has(self) -> None:
        """What lets the tab lead with the action rather than with a dash.

        The machine is sent rather than reimplemented in TypeScript, so a screen
        that has to decide between *Commission a visit* and *Book the visit* reads
        the server's answer.
        """
        inspection = (await build().build(make_claim())).inspection
        assert inspection.next_statuses == ["to_schedule"]

    @pytest.mark.asyncio
    async def test_an_unmatched_policy_leaves_the_excess_absent_not_zero(self) -> None:
        """A zero excess and an unknown excess are opposite facts."""
        financials = (await build().build(make_claim(policy_id=None))).financials
        assert financials.deductible is None

    @pytest.mark.asyncio
    async def test_an_unassigned_claim_leaves_the_authority_absent_not_zero(self) -> None:
        financials = (await build().build(make_claim())).financials
        assert financials.authority_limit is None

    @pytest.mark.asyncio
    async def test_the_currency_is_stated_even_when_every_figure_is_absent(self) -> None:
        """The screen needs something to total in that is not read off a null."""
        financials = (await build().build(make_claim(currency="EUR", policy_id=None))).financials
        assert financials.currency == "EUR"
        assert (financials.deductible, financials.authority_limit) == (None, None)

    @pytest.mark.asyncio
    async def test_an_empty_register_asserts_nothing_about_the_claim(self) -> None:
        """The register being empty is not a finding that there is nothing to recover.

        `unavailable_reason` used to carry the first statement and is now null,
        because the product does track recoveries. `no_recovery_reason` would carry
        the second — a handler's conclusion — and there is still nowhere to record
        one, so it stays null rather than being inferred from an empty list.
        """
        recoveries = (await build().build(make_claim())).recoveries
        assert recoveries.available is True
        assert recoveries.unavailable_reason is None
        assert recoveries.opportunities == []
        assert recoveries.no_recovery_reason is None


# ---------------------------------------------------------------------------
# Financials
# ---------------------------------------------------------------------------


class TestFinancials:
    @pytest.mark.asyncio
    async def test_reserve_lines_agree_with_the_ledger(self) -> None:
        movements = [
            movement("indemnity", 20_000_00, at=NOW),
            movement("indemnity", 40_000_00, at=NOW - timedelta(days=2)),
        ]
        financials = (
            await build(claims=FakeClaimRepo(movements=movements)).build(make_claim())
        ).financials

        line = next(row for row in financials.reserves if row.head == "indemnity")
        assert line.held.amount_minor == 60_000_00
        assert line.previous.amount_minor == 40_000_00

    @pytest.mark.asyncio
    async def test_untouched_heads_are_omitted_rather_than_shown_at_zero(self) -> None:
        movements = [movement("indemnity", 40_000_00, at=NOW)]
        financials = (
            await build(claims=FakeClaimRepo(movements=movements)).build(make_claim())
        ).financials
        assert [row.head for row in financials.reserves] == ["indemnity"]

    @pytest.mark.asyncio
    async def test_reserve_lines_are_ordered_by_the_enum_not_by_first_touch(self) -> None:
        """So the tab does not reorder itself as a claim develops."""
        movements = [
            movement("recovery", 5_000_00, at=NOW),
            movement("legal", 3_000_00, at=NOW - timedelta(days=1)),
            movement("indemnity", 40_000_00, at=NOW - timedelta(days=2)),
        ]
        financials = (
            await build(claims=FakeClaimRepo(movements=movements)).build(make_claim())
        ).financials
        assert [row.head for row in financials.reserves] == ["indemnity", "legal", "recovery"]

    @pytest.mark.asyncio
    async def test_every_movement_appears_as_a_transaction(self) -> None:
        movements = [
            movement("indemnity", 40_000_00, at=NOW, rationale="Initial schedule."),
            movement("expense", 1_200_00, at=NOW - timedelta(days=1), rationale="Adjuster's fee."),
        ]
        financials = (
            await build(claims=FakeClaimRepo(movements=movements)).build(make_claim())
        ).financials

        assert len(financials.transactions) == 2
        assert {row.kind for row in financials.transactions} == {"reserve"}
        assert financials.transactions[0].description == "Initial schedule."

    @pytest.mark.asyncio
    async def test_an_empty_ledger_is_a_real_answer(self) -> None:
        """`available` stays true: the claim genuinely holds nothing yet."""
        financials = (await build().build(make_claim())).financials
        assert financials.available is True
        assert financials.reserves == []
        assert financials.transactions == []

    @pytest.mark.asyncio
    async def test_the_excess_comes_from_the_matched_policy(self) -> None:
        policy = SimpleNamespace(deductible_amount_minor=25_000_00, currency="GBP")
        financials = (await build(policies=FakePolicyRepo(policy)).build(make_claim())).financials
        assert financials.deductible is not None
        assert financials.deductible.amount_minor == 25_000_00
        assert financials.deductible_applied is False

    @pytest.mark.asyncio
    async def test_the_authority_limit_comes_from_the_assigned_handler(self) -> None:
        handlers = FakeHandlerRepo(SimpleNamespace(authority_limit_minor=50_000_00, currency="GBP"))
        claims = FakeClaimRepo(
            assignment=SimpleNamespace(status=AssignmentStatus.ASSIGNED, handler_id="handler-1")
        )
        financials = (await build(claims=claims, handlers=handlers).build(make_claim())).financials
        assert financials.authority_limit is not None
        assert financials.authority_limit.amount_minor == 50_000_00

    @pytest.mark.asyncio
    async def test_the_estimate_prefers_the_extracted_loss_and_names_its_source(self) -> None:
        case = SimpleNamespace(
            id=uuid.uuid4(), estimated_loss_minor=1_200_000_00, repair_estimate_minor=90_000_00
        )
        financials = (await build(cases=FakeCaseRepo(case)).build(make_claim())).financials
        assert financials.estimate_total is not None
        assert financials.estimate_total.amount_minor == 1_200_000_00
        assert financials.estimate_source is not None
        assert "Estimated loss" in financials.estimate_source

    @pytest.mark.asyncio
    async def test_the_estimate_falls_back_on_the_repair_figure(self) -> None:
        case = SimpleNamespace(
            id=uuid.uuid4(), estimated_loss_minor=None, repair_estimate_minor=90_000_00
        )
        financials = (await build(cases=FakeCaseRepo(case)).build(make_claim())).financials
        assert financials.estimate_total is not None
        assert financials.estimate_source is not None
        assert "Repair estimate" in financials.estimate_source

    @pytest.mark.asyncio
    async def test_no_estimate_leaves_both_the_figure_and_its_source_absent(self) -> None:
        """An unattributed estimate is a number nobody can check."""
        financials = (await build().build(make_claim())).financials
        assert (financials.estimate_total, financials.estimate_source) == (None, None)


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


class TestAssessment:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (ClaimStatus.FNOL, "hold_pending_evidence"),
            (ClaimStatus.IN_REVIEW, "hold_pending_evidence"),
            (ClaimStatus.ESCALATED, "refer_to_manager"),
            (ClaimStatus.APPROVED, "settle"),
            (ClaimStatus.REJECTED, "decline"),
        ],
    )
    async def test_the_recommendation_is_derived_from_the_status(
        self, status: str, expected: str
    ) -> None:
        """Derived rather than stored, so the two cannot drift apart."""
        assessment = (await build().build(make_claim(status=status))).assessment
        assert assessment.recommendation == expected

    @pytest.mark.asyncio
    async def test_no_settlement_figure_before_a_settlement(self) -> None:
        """A figure on an undecided claim is a prediction dressed as a position."""
        assessment = (
            await build(
                claims=FakeClaimRepo(movements=[movement("indemnity", 40_000_00, at=NOW)])
            ).build(make_claim(status=ClaimStatus.IN_REVIEW))
        ).assessment
        assert assessment.recommended_settlement is None

    @pytest.mark.asyncio
    async def test_an_approved_claim_states_what_it_settled_at(self) -> None:
        assessment = (
            await build(
                claims=FakeClaimRepo(
                    movements=[
                        movement("indemnity", 40_000_00, at=NOW),
                        movement("recovery", 10_000_00, at=NOW),
                    ]
                )
            ).build(make_claim(status=ClaimStatus.APPROVED))
        ).assessment
        assert assessment.recommended_settlement is not None
        # The recovery is excluded: incurred, not net.
        assert assessment.recommended_settlement.amount_minor == 40_000_00

    @pytest.mark.asyncio
    async def test_liability_is_undetermined_and_says_so(self) -> None:
        assessment = (await build().build(make_claim())).assessment
        assert assessment.liability.position == "undetermined"
        assert assessment.liability.insured_share is None
        assert assessment.liability.rationale

    @pytest.mark.asyncio
    async def test_an_unknown_severity_does_not_become_critical(self) -> None:
        assessment = (await build().build(make_claim(severity=None))).assessment
        assert assessment.severity == "medium"


# ---------------------------------------------------------------------------
# Coverage sections, parties and the excess
# ---------------------------------------------------------------------------


class TestCoverageSections:
    @pytest.mark.asyncio
    async def test_sections_carry_their_proposal_and_their_override(self) -> None:
        """The four override fields are what make a position attributable."""
        rows = [
            coverage("fire", standpoint="excluded", proposed="confirmed", overridden=True),
            coverage("flood", standpoint="in_question", proposed="in_question"),
        ]
        assessment = (
            await build(claims=FakeClaimRepo(coverages=rows)).build(make_claim())
        ).assessment

        by_key = {section.id: section for section in assessment.coverage_sections}
        assert by_key["fire"].overridden is True
        assert by_key["fire"].proposed_standpoint == "confirmed"
        assert by_key["fire"].standpoint == "excluded"
        assert by_key["fire"].overridden_by == "R. Marsh"
        assert by_key["flood"].overridden is False

    @pytest.mark.asyncio
    async def test_parties_are_named_on_the_section_not_referenced(self) -> None:
        """A client-side join is how a name ends up under the wrong heading."""
        fire = coverage("fire")
        insured = party("Northline Logistics")
        rows = FakeClaimRepo(
            coverages=[fire],
            parties=[insured],
            links=[link(fire, insured, basis="Named insured")],
        )
        assessment = (await build(claims=rows).build(make_claim())).assessment

        section = assessment.coverage_sections[0]
        assert [p.name for p in section.parties] == ["Northline Logistics"]
        assert section.parties[0].basis == "Named insured"
        assert section.parties[0].role == "insured"

    @pytest.mark.asyncio
    async def test_a_section_with_no_parties_lists_none(self) -> None:
        assessment = (
            await build(claims=FakeClaimRepo(coverages=[coverage("fire")])).build(make_claim())
        ).assessment
        assert assessment.coverage_sections[0].parties == []

    @pytest.mark.asyncio
    async def test_a_link_to_a_missing_party_is_skipped_not_blanked(self) -> None:
        """Unreachable through the cascade; skipping is what keeps it unreachable."""
        fire = coverage("fire")
        ghost = party("Deleted Person")
        rows = FakeClaimRepo(coverages=[fire], parties=[], links=[link(fire, ghost)])
        assessment = (await build(claims=rows).build(make_claim())).assessment
        assert assessment.coverage_sections[0].parties == []

    @pytest.mark.asyncio
    async def test_every_party_on_the_claim_is_listed_for_editing(self) -> None:
        """So a section's links can be changed without a second read."""
        rows = FakeClaimRepo(parties=[party("Northline"), party("I. McGregor", role="other")])
        assessment = (await build(claims=rows).build(make_claim())).assessment
        assert {p.name for p in assessment.parties} == {"Northline", "I. McGregor"}

    @pytest.mark.asyncio
    async def test_exposure_sums_only_the_responding_sections(self) -> None:
        rows = FakeClaimRepo(
            coverages=[
                coverage("fire", standpoint="confirmed", claimed_minor=10_000_00),
                coverage("flood", standpoint="excluded", claimed_minor=99_000_00),
                coverage("bi", standpoint="in_question", claimed_minor=50_000_00),
            ]
        )
        assessment = (await build(claims=rows).build(make_claim())).assessment
        assert assessment.exposure.amount_minor == 10_000_00

    @pytest.mark.asyncio
    async def test_over_limit_sections_are_named(self) -> None:
        rows = FakeClaimRepo(
            coverages=[coverage("fire", limit_minor=5_000_00, claimed_minor=12_000_00)]
        )
        assessment = (await build(claims=rows).build(make_claim())).assessment
        assert assessment.over_limit_sections == ["fire"]

    @pytest.mark.asyncio
    async def test_a_claim_with_no_policy_has_no_sections_and_is_still_available(self) -> None:
        """An empty contract is a real answer about the claim, not a missing feature."""
        assessment = (await build().build(make_claim(policy_id=None))).assessment
        assert assessment.available is True
        assert assessment.coverage_sections == []
        assert assessment.exposure.amount_minor == 0


class TestDeductibles:
    @pytest.mark.asyncio
    async def test_the_headline_excess_is_the_contract_level_row(self) -> None:
        fire = coverage("fire")
        rows = FakeClaimRepo(
            coverages=[fire],
            deductibles=[
                deductible(amount_minor=25_000_00),
                deductible(coverage_row=fire, amount_minor=5_000_00),
            ],
        )
        financials = (await build(claims=rows).build(make_claim())).financials

        assert financials.deductible is not None
        assert financials.deductible.amount_minor == 25_000_00
        assert len(financials.deductibles) == 2

    @pytest.mark.asyncio
    async def test_a_section_excess_names_its_section(self) -> None:
        fire = coverage("fire")
        rows = FakeClaimRepo(
            coverages=[fire], deductibles=[deductible(coverage_row=fire, amount_minor=5_000_00)]
        )
        financials = (await build(claims=rows).build(make_claim())).financials
        assert financials.deductibles[0].section_key == "fire"

    @pytest.mark.asyncio
    async def test_the_remaining_figure_comes_from_the_erosion_read(self) -> None:
        rows = FakeClaimRepo(
            deductibles=[deductible(amount_minor=25_000_00, applied_minor=4_000_00)]
        )
        financials = (await build(claims=rows).build(make_claim())).financials

        excess = financials.deductibles[0]
        assert excess.applied.amount_minor == 4_000_00
        assert excess.remaining.amount_minor == 21_000_00
        assert excess.exhausted is False

    @pytest.mark.asyncio
    async def test_applied_becomes_true_only_when_something_was_taken(self) -> None:
        """Read from the rows now rather than hard-coded false, so it becomes true
        on its own the day a payment carries the excess."""
        unapplied = FakeClaimRepo(deductibles=[deductible(applied_minor=0)])
        applied = FakeClaimRepo(deductibles=[deductible(applied_minor=1_00)])

        assert (
            await build(claims=unapplied).build(make_claim())
        ).financials.deductible_applied is False
        assert (
            await build(claims=applied).build(make_claim())
        ).financials.deductible_applied is True

    @pytest.mark.asyncio
    async def test_the_claws_type_is_carried_through(self) -> None:
        rows = FakeClaimRepo(deductibles=[deductible(deductible_type="aggregate")])
        financials = (await build(claims=rows).build(make_claim())).financials
        assert financials.deductibles[0].deductible_type == "aggregate"

    @pytest.mark.asyncio
    async def test_with_no_claim_excess_the_policy_figure_is_the_fallback(self) -> None:
        """A claim created before this table existed still shows an excess."""
        policy_row = SimpleNamespace(deductible_amount_minor=30_000_00, currency="GBP")
        financials = (
            await build(policies=FakePolicyRepo(policy_row)).build(make_claim())
        ).financials
        assert financials.deductible is not None
        assert financials.deductible.amount_minor == 30_000_00
        assert financials.deductibles == []


# ---------------------------------------------------------------------------
# Fraud
# ---------------------------------------------------------------------------


class TestFraud:
    @pytest.mark.asyncio
    async def test_the_red_flags_are_the_pipeline_s_own_indicators(self) -> None:
        """Read from the stored analysis, so it cannot disagree with the header."""
        analysis = SimpleNamespace(
            result={
                "level": "medium",
                "score": 0.4,
                "indicators": [
                    {
                        "code": "FF-14",
                        "title": "Late notification",
                        "detail": "Reported 31 days after the loss.",
                        "weight": 0.3,
                    }
                ],
            }
        )
        fraud = (await build(cases=FakeCaseRepo(analysis=analysis)).build(make_claim())).fraud
        assert [flag.code for flag in fraud.red_flags] == ["FF-14"]
        assert fraud.red_flags[0].weight == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_the_siu_status_no_longer_tracks_the_review_flag(self) -> None:
        """The defect this replaced: a flagged claim reported `screening`.

        Which asserted that somebody was looking when nothing but the model had.
        A case exists because a person opened one, so a flagged claim with no case
        is `not_referred` exactly like an unflagged one.
        """
        flagged = (await build().build(make_claim(fraud_flag=True))).fraud
        clear = (await build().build(make_claim(fraud_flag=False))).fraud
        assert flagged.siu_status == "not_referred"
        assert clear.siu_status == "not_referred"

    @pytest.mark.asyncio
    async def test_a_referred_claim_reports_its_case(self) -> None:
        case = SimpleNamespace(
            status="under_investigation",
            referred_by="R. Achebe",
            referred_at=NOW,
            investigator="D. Mensah (SIU)",
            siu_reference="SIU-2026-0451",
            referral_reason="Third notified loss at this site in eleven months.",
            outcome=None,
            recommended_actions=[],
        )
        fraud = (await build(claims=FakeClaimRepo(siu_case=case)).build(make_claim())).fraud

        assert fraud.siu_status == "under_investigation"
        assert fraud.investigator == "D. Mensah (SIU)"
        assert fraud.siu_reference == "SIU-2026-0451"
        assert "eleven months" in (fraud.referral_reason or "")
        #: The machine is sent rather than reimplemented on the screen.
        assert fraud.next_statuses == ["closed_confirmed", "closed_no_action"]

    @pytest.mark.asyncio
    async def test_a_disposed_indicator_carries_its_verdict(self) -> None:
        """The write that made verdicts survive a reload.

        They were held in a working copy on the client, so a handler who worked
        through eight indicators lost all eight by refreshing.
        """
        analysis = SimpleNamespace(
            result={"indicators": [{"code": "FF-14", "title": "Late notification", "weight": 0.3}]}
        )
        disposition = SimpleNamespace(
            code="FF-14",
            disposition="discounted",
            note="Broker's own delay, evidenced in the covering email.",
            reviewed_by="R. Achebe",
            reviewed_at=NOW,
        )
        fraud = (
            await build(
                cases=FakeCaseRepo(analysis=analysis),
                claims=FakeClaimRepo(dispositions=[disposition]),
            ).build(make_claim())
        ).fraud

        flag = fraud.red_flags[0]
        assert flag.disposition == "discounted"
        assert flag.reviewed_by == "R. Achebe"
        assert "Broker's own delay" in (flag.disposition_note or "")
        assert fraud.undecided_indicators == 0

    @pytest.mark.asyncio
    async def test_an_undecided_indicator_is_counted_as_undecided(self) -> None:
        """Absence is the third state — see `app.domain.siu`."""
        analysis = SimpleNamespace(
            result={
                "indicators": [
                    {"code": "FF-14", "title": "Late notification", "weight": 0.3},
                    {"code": "MV-03", "title": "Prior at this site", "weight": 0.2},
                ]
            }
        )
        fraud = (await build(cases=FakeCaseRepo(analysis=analysis)).build(make_claim())).fraud

        assert fraud.undecided_indicators == 2
        assert all(flag.disposition is None for flag in fraud.red_flags)

    @pytest.mark.asyncio
    async def test_a_clean_claim_says_there_is_nothing_outstanding(self) -> None:
        """Rather than an empty list, which reads as a rendering fault.

        The actions used to be three lines of static procedure, shown whenever there
        were indicators. They are derived from the case's own state now — see
        `app.domain.siu.recommended_actions` — so a claim with nothing to do says so
        in one line.
        """
        fraud = (await build().build(make_claim())).fraud
        assert fraud.recommended_actions == ["Nothing outstanding on the fraud review."]

    @pytest.mark.asyncio
    async def test_prior_claims_on_the_policy_are_listed(self) -> None:
        sibling = make_claim(
            reference="CLM-000002",
            status=ClaimStatus.APPROVED,
            paid_minor=15_000_00,
            closed_at=NOW - timedelta(days=200),
            loss_type="water_damage",
        )
        fraud = (await build(claims=FakeClaimRepo(siblings=[sibling])).build(make_claim())).fraud

        assert len(fraud.prior_claims) == 1
        prior = fraud.prior_claims[0]
        assert (prior.reference, prior.outcome, prior.loss_type) == (
            "CLM-000002",
            "Settled",
            "water damage",
        )
        assert prior.settled_amount is not None

    @pytest.mark.asyncio
    async def test_only_a_flagged_prior_claim_is_material(self) -> None:
        """A history where every line is highlighted is a history nobody reads."""
        clean = make_claim(reference="CLM-000002", fraud_flag=False)
        flagged = make_claim(reference="CLM-000003", fraud_flag=True)
        fraud = (
            await build(claims=FakeClaimRepo(siblings=[clean, flagged])).build(make_claim())
        ).fraud
        assert {p.reference: p.material for p in fraud.prior_claims} == {
            "CLM-000002": False,
            "CLM-000003": True,
        }

    @pytest.mark.asyncio
    async def test_an_open_prior_claim_has_no_settlement(self) -> None:
        sibling = make_claim(reference="CLM-000002", status=ClaimStatus.IN_REVIEW)
        prior = (
            await build(claims=FakeClaimRepo(siblings=[sibling])).build(make_claim())
        ).fraud.prior_claims[0]
        assert (prior.outcome, prior.settled_at, prior.settled_amount) == ("Open", None, None)


# ---------------------------------------------------------------------------
# The activity log
# ---------------------------------------------------------------------------


class TestActivityLog:
    @pytest.mark.asyncio
    async def test_events_are_categorised_and_the_material_ones_marked(self) -> None:
        events = [
            event(AuditEventType.RESERVE_MOVED),
            event(AuditEventType.PIPELINE_STARTED),
        ]
        activity = (await build(audit=FakeAudit(events)).build(make_claim())).activity

        assert [(row.category, row.material) for row in activity] == [
            ("financial", True),
            ("intake", False),
        ]

    @pytest.mark.asyncio
    async def test_a_model_s_action_reads_as_the_assistant_not_the_platform(self) -> None:
        """The conflation `actor_type` exists to prevent."""
        events = [event(AuditEventType.EXTRACTION_COMPLETED, actor_type=ActorType.AI)]
        activity = (await build(audit=FakeAudit(events)).build(make_claim())).activity
        assert activity[0].actor_kind == "assistant"

    @pytest.mark.asyncio
    async def test_a_diff_becomes_a_sentence(self) -> None:
        events = [
            event(
                AuditEventType.RESERVE_MOVED,
                before={"reserve_minor": 4_000_000},
                after={"reserve_minor": 6_000_000},
            )
        ]
        activity = (await build(audit=FakeAudit(events)).build(make_claim())).activity
        assert activity[0].detail == "reserve minor 4000000 → 6000000"

    @pytest.mark.asyncio
    async def test_an_event_with_nothing_to_diff_has_no_detail(self) -> None:
        events = [event(AuditEventType.CLAIM_NOTE_ADDED)]
        activity = (await build(audit=FakeAudit(events)).build(make_claim())).activity
        assert activity[0].detail is None

    @pytest.mark.asyncio
    async def test_an_unchanged_field_is_not_reported_as_a_change(self) -> None:
        events = [
            event(
                AuditEventType.CLAIM_DECIDED,
                before={"status": "in_review"},
                after={"status": "in_review"},
            )
        ]
        activity = (await build(audit=FakeAudit(events)).build(make_claim())).activity
        assert activity[0].detail is None


# ---------------------------------------------------------------------------
# Notes across the tabs
# ---------------------------------------------------------------------------


class TestSectionNotes:
    @pytest.mark.asyncio
    async def test_each_tab_gets_only_its_own_notes(self) -> None:
        notes = [
            note("inspection", "Chased the adjuster.", at=NOW),
            note("assessment", "Accepting the schedule.", at=NOW),
            note("fraud", "Late notification explained.", at=NOW),
            note("general", "Broker called.", at=NOW),
        ]
        sections = await build(claims=FakeClaimRepo(notes=notes)).build(make_claim())

        assert [n.body for n in sections.inspection.notes] == ["Chased the adjuster."]
        assert [n.body for n in sections.assessment.notes] == ["Accepting the schedule."]
        assert [n.body for n in sections.fraud.notes] == ["Late notification explained."]

    @pytest.mark.asyncio
    async def test_notes_read_oldest_first_within_a_tab(self) -> None:
        """A conversation reads forwards; the repository returns newest first."""
        notes = [
            note("inspection", "second", at=NOW),
            note("inspection", "first", at=NOW - timedelta(hours=2)),
        ]
        inspection = (await build(claims=FakeClaimRepo(notes=notes)).build(make_claim())).inspection
        assert [n.body for n in inspection.notes] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_a_note_on_recoveries_is_read_back(self) -> None:
        """`NoteSection.RECOVERY` is writable, so it needs somewhere to be read.

        Without this the section had no `notes` field at all: a note filed against
        recoveries was stored and audited and never shown again, which is a write
        path to a place nothing reads.
        """
        notes = [note("recovery", "Instructed solicitors on the subrogation.", at=NOW)]
        recoveries = (await build(claims=FakeClaimRepo(notes=notes)).build(make_claim())).recoveries

        assert recoveries.available is True
        assert [n.body for n in recoveries.notes] == ["Instructed solicitors on the subrogation."]

    @pytest.mark.asyncio
    async def test_every_writable_note_section_is_read_back_somewhere(self) -> None:
        """The guard against adding a sixth section nobody renders.

        Every value of `NoteSection` has to surface on some section of the payload,
        or the API accepts a note it will never show. `general` is the exception by
        design — it belongs to the claim rather than to a tab, and the claim detail
        carries it.
        """
        from app.domain.enums import NoteSection

        bodies = {section: f"note on {section}" for section in NoteSection}
        notes = [note(str(section), body, at=NOW) for section, body in bodies.items()]
        sections = await build(claims=FakeClaimRepo(notes=notes)).build(make_claim())

        rendered = {
            n.body
            for section in (
                sections.inspection,
                sections.assessment,
                sections.fraud,
                sections.recoveries,
            )
            for n in section.notes
        }
        for section, body in bodies.items():
            if section is NoteSection.GENERAL:
                continue
            assert body in rendered, f"{section} is writable and rendered nowhere"

    @pytest.mark.asyncio
    async def test_a_section_with_no_record_still_carries_its_notes(self) -> None:
        """A note written before the visit was commissioned survives it.

        Notes hang off the claim and not off the inspection, which is what lets
        "chased the adjuster, still no date" exist before there is anything to
        chase — and what stops a commissioned visit orphaning it.
        """
        notes = [note("inspection", "Visit moved to Thursday.", at=NOW)]
        inspection = (await build(claims=FakeClaimRepo(notes=notes)).build(make_claim())).inspection
        assert inspection.status == "not_commissioned"
        assert [n.body for n in inspection.notes] == ["Visit moved to Thursday."]

    @pytest.mark.asyncio
    async def test_an_empty_register_is_a_state_rather_than_a_gap(self) -> None:
        """A claim with nothing to recover, said as such.

        The two totals are **absent** rather than zero. A register holding nothing
        and a register that expects nothing back are different readings, and £0
        against `expected` would state the second about every claim nobody has
        looked at.
        """
        recoveries = (await build().build(make_claim())).recoveries

        assert recoveries.available is True
        assert recoveries.opportunities == []
        assert recoveries.expected_total is None
        assert recoveries.recovered_total is None
        assert recoveries.limitation_warnings == []

    @pytest.mark.asyncio
    async def test_an_unreferred_claim_reports_no_investigation(self) -> None:
        """`not_referred` is a state, and it is not derived from the fraud flag.

        It used to be: a flagged claim reported `screening`, which asserted that
        somebody was looking when nothing but the model had.
        """
        fraud = (await build().build(make_claim(fraud_flag=True))).fraud

        assert fraud.available is True
        assert fraud.siu_status == "not_referred"
        assert fraud.referred_by is None
        assert fraud.investigator is None
