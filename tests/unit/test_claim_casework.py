"""Claim casework: the three writes, tested against in-memory repositories.

The doubles here are deliberately not mocks. Each one keeps real state and answers
real queries, because every test in this file is about a *consequence* — the reserve
cache moved with the ledger, the authority flag was re-derived, the blocked attempt
was audited — and a mock that only records calls cannot see a consequence.

Nothing commits. The service does not commit either, which is the point: the route
owns the transaction, so these tests exercise exactly what production does up to the
commit boundary and no further.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import ConflictError, ValidationError
from app.domain.enums import (
    AssignmentStatus,
    AuditEventType,
    ClaimDecisionAction,
    ClaimStatus,
    MovementType,
    NoteSection,
    SiuStatus,
)
from app.services.claims.casework import ClaimCaseworkService


class FakeClaimRepository:
    """Notes, movements, the assignment and the fraud review, held in lists.

    `list_movements` returns newest-first, matching the real query's ordering —
    which the ledger arithmetic depends on, so a double that returned insertion
    order would make `previous_held` pass here and fail in production.

    `fraud_dispositions` and `siu_case` are here because **every** decision reads
    them: `ClaimCaseworkService.decide` calls `fraud_gate`, which is what replaced
    the old `claim.fraud_flag` boolean. Both default to empty, which is the
    correct default rather than a convenient one — a claim with no fraud analysis
    behind it has nothing outstanding to decide, and inventing a block for
    evidence that does not exist is the mirror of the bug the gate replaced.
    """

    def __init__(
        self,
        assignment: object | None = None,
        *,
        siu_case: object | None = None,
    ) -> None:
        self.notes: list[Any] = []
        self.movements: list[Any] = []
        self.fraud_dispositions: list[Any] = []
        self.siu_case = siu_case
        self._assignment = assignment

    def add_note(self, claim_id: uuid.UUID, *, author: str, body: str, section: str) -> Any:
        note = SimpleNamespace(
            id=uuid.uuid4(),
            claim_id=claim_id,
            author=author,
            body=body,
            section=section,
            created_at=datetime.now(UTC),
        )
        self.notes.append(note)
        return note

    def add_movement(self, movement: Any) -> Any:
        self.movements.append(movement)
        return movement

    async def list_movements(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(reversed(self.movements))

    async def list_notes(self, claim_id: uuid.UUID, *, section: str | None = None) -> list[Any]:
        del claim_id
        rows = list(reversed(self.notes))
        return [row for row in rows if section is None or row.section == section]

    async def get_assignment(self, claim_id: uuid.UUID) -> object | None:
        del claim_id
        return self._assignment

    async def list_fraud_dispositions(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self.fraud_dispositions)

    async def get_siu_case(self, claim_id: uuid.UUID) -> object | None:
        del claim_id
        return self.siu_case


class FakeHandlerRepository:
    def __init__(self, authority_limit_minor: int | None = None) -> None:
        self.handler = SimpleNamespace(
            id="handler-1",
            authority_limit_minor=authority_limit_minor,
            currency="GBP",
            full_name="R. Marsh",
        )

    async def get(self, handler_id: Any) -> object | None:
        del handler_id
        return self.handler


class RecordingAudit:
    """The audit service's `claim` method, recording rather than writing."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def claim(self, claim: Any, *, event_type: Any, summary: str, actor: str, **rest: Any) -> Any:
        event = {
            "event_type": str(event_type),
            "summary": summary,
            "actor": actor,
            "reference": claim.reference,
            **rest,
        }
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        return [event["event_type"] for event in self.events]


def make_claim(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "CLM-000001",
        "status": ClaimStatus.IN_REVIEW,
        "reserve_minor": 0,
        "paid_minor": 0,
        "currency": "GBP",
        "fraud_flag": False,
        "over_authority": False,
        "closed_at": None,
        "handler_name": "R. Marsh",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build(
    *,
    assignment: object | None = None,
    authority_limit_minor: int | None = None,
) -> tuple[ClaimCaseworkService, FakeClaimRepository, RecordingAudit]:
    claims = FakeClaimRepository(assignment)
    audit = RecordingAudit()
    service = ClaimCaseworkService(
        claims,  # type: ignore[arg-type]
        FakeHandlerRepository(authority_limit_minor),  # type: ignore[arg-type]
        audit,  # type: ignore[arg-type]
    )
    return service, claims, audit


def assigned() -> SimpleNamespace:
    return SimpleNamespace(status=AssignmentStatus.ASSIGNED, handler_id="handler-1")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class TestNotes:
    @pytest.mark.asyncio
    async def test_a_note_is_written_and_audited(self) -> None:
        service, claims, audit = build()
        claim = make_claim()

        note = await service.add_note(
            claim, body="Chased the adjuster.", section=NoteSection.INSPECTION, actor="R. Marsh"
        )

        assert note.body == "Chased the adjuster."
        assert claims.notes == [note]
        assert audit.types() == [AuditEventType.CLAIM_NOTE_ADDED]

    @pytest.mark.asyncio
    async def test_the_body_is_not_copied_into_the_audit_event(self) -> None:
        """One durable copy of the sentence, so there is one place to redact from."""
        service, _, audit = build()

        await service.add_note(
            make_claim(),
            body="Claimant's medical detail follows.",
            section=NoteSection.GENERAL,
            actor="R. Marsh",
        )

        recorded = repr(audit.events[0])
        assert "medical detail" not in recorded

    @pytest.mark.asyncio
    async def test_the_section_is_recorded_on_the_event(self) -> None:
        service, _, audit = build()
        await service.add_note(make_claim(), body="x", section=NoteSection.FRAUD, actor="R. Marsh")
        assert audit.events[0]["context"]["section"] == "fraud"


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class TestReserveMovements:
    @pytest.mark.asyncio
    async def test_a_movement_moves_the_cached_reserve_with_it(self) -> None:
        """The obligation the whole service exists for."""
        service, claims, _ = build()
        claim = make_claim(reserve_minor=0)

        await service.post_movement(
            claim,
            movement_type=MovementType.INDEMNITY,
            amount_minor=40_000_00,
            rationale="Adjuster's initial schedule.",
            actor="R. Marsh",
        )

        assert claim.reserve_minor == 40_000_00
        assert len(claims.movements) == 1

    @pytest.mark.asyncio
    async def test_movements_accumulate_rather_than_replace(self) -> None:
        service, _, _ = build()
        claim = make_claim(reserve_minor=0)

        for amount in (40_000_00, 20_000_00, -5_000_00):
            await service.post_movement(
                claim,
                movement_type=MovementType.INDEMNITY,
                amount_minor=amount,
                rationale="Revised.",
                actor="R. Marsh",
            )

        assert claim.reserve_minor == 55_000_00

    @pytest.mark.asyncio
    async def test_a_release_below_zero_is_refused_with_the_figures(self) -> None:
        """A 422 naming both figures, not a 500 naming a constraint."""
        service, claims, _ = build()
        claim = make_claim(reserve_minor=10_000_00)

        with pytest.raises(ValidationError) as raised:
            await service.post_movement(
                claim,
                movement_type=MovementType.INDEMNITY,
                amount_minor=-15_000_00,
                rationale="Over-release.",
                actor="R. Marsh",
            )

        assert "1000000" in str(raised.value)
        assert claims.movements == []
        assert claim.reserve_minor == 10_000_00

    @pytest.mark.asyncio
    async def test_a_recovery_is_recorded_without_moving_the_reserve(self) -> None:
        """Money coming back is tracked against the reserve, not netted out of it."""
        service, claims, audit = build()
        claim = make_claim(reserve_minor=40_000_00)

        await service.post_movement(
            claim,
            movement_type=MovementType.RECOVERY,
            amount_minor=15_000_00,
            rationale="Third party's insurer accepted liability.",
            actor="R. Marsh",
        )

        assert claim.reserve_minor == 40_000_00
        assert len(claims.movements) == 1
        assert audit.types() == [AuditEventType.RECOVERY_RECORDED]

    @pytest.mark.asyncio
    async def test_a_reserve_move_and_a_recovery_are_different_events(self) -> None:
        service, _, audit = build()
        claim = make_claim()

        await service.post_movement(
            claim,
            movement_type=MovementType.INDEMNITY,
            amount_minor=1_00,
            rationale="x",
            actor="a",
        )
        await service.post_movement(
            claim,
            movement_type=MovementType.RECOVERY,
            amount_minor=1_00,
            rationale="x",
            actor="a",
        )

        assert audit.types() == [
            AuditEventType.RESERVE_MOVED,
            AuditEventType.RECOVERY_RECORDED,
        ]

    @pytest.mark.asyncio
    async def test_the_reserve_currency_defaults_to_the_claim_s(self) -> None:
        service, claims, _ = build()
        await service.post_movement(
            make_claim(currency="EUR"),
            movement_type=MovementType.INDEMNITY,
            amount_minor=1_00,
            rationale="x",
            actor="a",
        )
        assert claims.movements[0].currency == "EUR"

    @pytest.mark.asyncio
    async def test_an_explicit_currency_is_upper_cased_and_kept(self) -> None:
        service, claims, _ = build()
        await service.post_movement(
            make_claim(currency="GBP"),
            movement_type=MovementType.INDEMNITY,
            amount_minor=1_00,
            rationale="x",
            actor="a",
            currency="usd",
            accounting_amount_minor=80,
            accounting_currency="gbp",
        )
        movement = claims.movements[0]
        assert (movement.currency, movement.accounting_currency) == ("USD", "GBP")

    @pytest.mark.asyncio
    async def test_a_decided_claim_refuses_a_movement(self) -> None:
        service, claims, _ = build()

        with pytest.raises(ConflictError):
            await service.post_movement(
                make_claim(status=ClaimStatus.APPROVED),
                movement_type=MovementType.INDEMNITY,
                amount_minor=1_00,
                rationale="Restatement.",
                actor="a",
            )

        assert claims.movements == []

    @pytest.mark.asyncio
    async def test_raising_past_the_limit_re_derives_the_authority_flag(self) -> None:
        """Left stale, a claim raised past its limit keeps a green decision bar."""
        service, _, _ = build(assignment=assigned(), authority_limit_minor=50_000_00)
        claim = make_claim(reserve_minor=0, over_authority=False)

        await service.post_movement(
            claim,
            movement_type=MovementType.INDEMNITY,
            amount_minor=60_000_00,
            rationale="Revised upward after the survey.",
            actor="R. Marsh",
        )

        assert claim.over_authority is True

    @pytest.mark.asyncio
    async def test_releasing_back_under_the_limit_clears_the_flag(self) -> None:
        service, _, _ = build(assignment=assigned(), authority_limit_minor=50_000_00)
        claim = make_claim(reserve_minor=60_000_00, over_authority=True)

        await service.post_movement(
            claim,
            movement_type=MovementType.INDEMNITY,
            amount_minor=-20_000_00,
            rationale="Scope reduced.",
            actor="R. Marsh",
        )

        assert claim.over_authority is False


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class TestDecisions:
    @pytest.mark.asyncio
    async def test_an_approval_moves_the_status_and_stamps_the_closure(self) -> None:
        service, _, audit = build(assignment=assigned(), authority_limit_minor=50_000_00)
        claim = make_claim(reserve_minor=10_000_00)

        await service.decide(
            claim,
            decision=ClaimDecisionAction.APPROVE_SETTLEMENT,
            actor="R. Marsh",
            reason="Adjuster's figure accepted.",
        )

        assert claim.status == ClaimStatus.APPROVED
        assert claim.closed_at is not None
        assert audit.types() == [AuditEventType.CLAIM_DECIDED]

    @pytest.mark.asyncio
    async def test_a_non_closing_decision_leaves_the_claim_open(self) -> None:
        service, _, _ = build()
        claim = make_claim()

        await service.decide(
            claim, decision=ClaimDecisionAction.REQUEST_INFORMATION, actor="R. Marsh"
        )

        assert claim.status == ClaimStatus.IN_REVIEW
        assert claim.closed_at is None

    @pytest.mark.asyncio
    async def test_the_verb_is_kept_in_the_audit_context(self) -> None:
        """Two verbs share `ESCALATED`; only the context tells them apart."""
        service, _, audit = build()

        await service.decide(
            make_claim(), decision=ClaimDecisionAction.REFER_TO_MANAGER, actor="R. Marsh"
        )

        assert audit.events[0]["context"]["decision"] == "refer_to_manager"

    @pytest.mark.asyncio
    async def test_a_blocked_approval_is_refused_and_audited(self) -> None:
        """The attempt is a fact worth keeping, not something to swallow."""
        service, _, audit = build(assignment=assigned(), authority_limit_minor=50_000_00)
        claim = make_claim(reserve_minor=60_000_00)

        with pytest.raises(ValidationError) as raised:
            await service.decide(
                claim,
                decision=ClaimDecisionAction.APPROVE_SETTLEMENT,
                actor="R. Marsh",
                reason="Trying anyway.",
            )

        assert claim.status == ClaimStatus.IN_REVIEW
        assert audit.types() == [AuditEventType.CLAIM_DECISION_BLOCKED]
        assert "above your authority" in str(raised.value.details["blocks"][0])

    @pytest.mark.asyncio
    async def test_the_authority_check_reads_the_ledger_not_the_cache(self) -> None:
        """A claim whose cached reserve is stale must still be caught.

        The cache says £40,000 and the ledger says £55,000 incurred. Against a
        £50,000 authority only the ledger's answer blocks — which is why the
        service re-reads it rather than trusting `reserve_minor`.
        """
        service, claims, _ = build(assignment=assigned(), authority_limit_minor=50_000_00)
        claim = make_claim(reserve_minor=40_000_00)
        claims.movements = [
            SimpleNamespace(movement_type="indemnity", amount_minor=52_000_00),
            SimpleNamespace(movement_type="expense", amount_minor=3_000_00),
        ]

        with pytest.raises(ValidationError):
            await service.decide(
                claim,
                decision=ClaimDecisionAction.APPROVE_SETTLEMENT,
                actor="R. Marsh",
                reason="x",
            )

    @pytest.mark.asyncio
    async def test_a_referral_is_allowed_on_a_blocked_claim(self) -> None:
        """Otherwise a claim under SIU investigation could not be moved at all.

        Previously set up with `fraud_flag=True`, which stopped blocking anything
        when `fraud_gate` replaced that boolean — so the test went on passing
        while no longer creating the condition its name describes. The block is
        now a real one, and the first half of the test proves it *is* a block
        before the second half shows a referral getting through it.

        Everything else is deliberately unblocked — assigned, and well inside
        authority — so the open SIU case is the only thing in the way.
        """
        service, claims, _ = build(assignment=assigned(), authority_limit_minor=50_000_00)
        claims.siu_case = SimpleNamespace(status=SiuStatus.UNDER_INVESTIGATION)

        with pytest.raises(ValidationError):
            await service.decide(
                make_claim(reserve_minor=1_000_00),
                decision=ClaimDecisionAction.APPROVE_SETTLEMENT,
                actor="R. Marsh",
                reason="x",
            )

        claim = make_claim(reserve_minor=1_000_00)
        await service.decide(claim, decision=ClaimDecisionAction.REFER_TO_MANAGER, actor="R. Marsh")

        assert claim.status == ClaimStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_a_decided_claim_cannot_be_decided_again(self) -> None:
        service, _, audit = build(assignment=assigned())

        with pytest.raises(ConflictError):
            await service.decide(
                make_claim(status=ClaimStatus.REJECTED),
                decision=ClaimDecisionAction.APPROVE_SETTLEMENT,
                actor="R. Marsh",
                reason="x",
            )

        assert audit.events == []

    @pytest.mark.asyncio
    async def test_an_illegal_transition_is_a_conflict(self) -> None:
        """A fresh claim cannot be settled without passing through review."""
        service, _, _ = build(assignment=assigned(), authority_limit_minor=None)
        claim = make_claim(status=ClaimStatus.FNOL)

        with pytest.raises(ConflictError):
            await service.decide(
                claim,
                decision=ClaimDecisionAction.APPROVE_SETTLEMENT,
                actor="R. Marsh",
                reason="x",
            )

        assert claim.status == ClaimStatus.FNOL
