"""The claim's own rules, tested without a database or an ORM.

Everything in `app.domain.claim_lifecycle` is a pure function over plain objects,
so these tests build claims, assignments and movements out of `SimpleNamespace`.
That is not a shortcut — it is the property the module exists to have, and a test
that needed a session to check whether a settlement is blocked would be evidence
the rule had leaked into the service layer.

The cases worth reading as a group are the authority ones. Three of them together
say the thing that matters: a limit only applies to somebody who is actually on
the file, the ledger beats the cached reserve when the two disagree, and a
non-committing decision is never blocked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain import claim_lifecycle as lifecycle
from app.domain.claim_lifecycle import BlockCode
from app.domain.enums import (
    ActivityCategory,
    AssignmentStatus,
    AuditEventType,
    ClaimDecisionAction,
    ClaimStatus,
    MovementType,
)


def make_claim(**overrides: object) -> SimpleNamespace:
    """A claim with every attribute the rules read, defaulted to benign."""
    base: dict[str, object] = {
        "reference": "CLM-000001",
        "status": ClaimStatus.IN_REVIEW,
        "reserve_minor": 0,
        "fraud_flag": False,
        "over_authority": False,
        "currency": "GBP",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def assigned() -> SimpleNamespace:
    return SimpleNamespace(status=AssignmentStatus.ASSIGNED, handler_id="handler-1")


def recommended() -> SimpleNamespace:
    return SimpleNamespace(status=AssignmentStatus.RECOMMENDED, handler_id="handler-1")


def movement(movement_type: MovementType, amount_minor: int) -> SimpleNamespace:
    return SimpleNamespace(movement_type=str(movement_type), amount_minor=amount_minor)


def codes(blocks: list[lifecycle.ApprovalBlock]) -> set[str]:
    """The blocks as their codes.

    Asserted on rather than the sentences: the code is the contract a screen acts
    on, and the wording is meant to be improvable without breaking a test.
    """
    return {str(block.code) for block in blocks}


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_the_happy_path_is_legal(self) -> None:
        assert lifecycle.can_transition(ClaimStatus.FNOL, ClaimStatus.IN_REVIEW)
        assert lifecycle.can_transition(ClaimStatus.IN_REVIEW, ClaimStatus.APPROVED)

    def test_a_decided_claim_cannot_move(self) -> None:
        for terminal in (ClaimStatus.APPROVED, ClaimStatus.REJECTED):
            assert lifecycle.is_terminal(terminal)
            assert lifecycle.allowed_transitions(terminal) == frozenset()
            assert not lifecycle.can_transition(terminal, ClaimStatus.IN_REVIEW)

    def test_in_review_can_stay_where_it_is(self) -> None:
        """What `request_information` needs: a real decision that moves nothing."""
        assert lifecycle.can_transition(ClaimStatus.IN_REVIEW, ClaimStatus.IN_REVIEW)

    def test_an_escalation_can_come_back_down(self) -> None:
        """A manager who decides a referral was unnecessary has to be able to undo it."""
        assert lifecycle.can_transition(ClaimStatus.ESCALATED, ClaimStatus.IN_REVIEW)

    def test_a_fresh_claim_cannot_jump_straight_to_approved(self) -> None:
        assert not lifecycle.can_transition(ClaimStatus.FNOL, ClaimStatus.APPROVED)

    def test_an_unknown_status_refuses_rather_than_raises(self) -> None:
        """A row written by an older revision is a thing to refuse, not to crash on."""
        assert not lifecycle.can_transition("mystery", ClaimStatus.APPROVED)
        assert not lifecycle.can_transition(ClaimStatus.IN_REVIEW, "mystery")
        assert lifecycle.allowed_transitions("mystery") == frozenset()


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class TestDecisions:
    def test_every_verb_maps_to_a_status(self) -> None:
        """Total by construction — `status_for_decision` has no fallback."""
        for decision in ClaimDecisionAction:
            assert isinstance(lifecycle.status_for_decision(decision), ClaimStatus)

    def test_the_two_escalating_verbs_share_a_status_and_stay_distinct(self) -> None:
        send = ClaimDecisionAction.SEND_TO_APPROVAL
        refer = ClaimDecisionAction.REFER_TO_MANAGER
        assert lifecycle.status_for_decision(send) == lifecycle.status_for_decision(refer)
        assert lifecycle.decision_phrase(send) != lifecycle.decision_phrase(refer)

    def test_only_the_two_closing_verbs_commit(self) -> None:
        assert set(lifecycle.COMMITTING_DECISIONS) == {
            ClaimDecisionAction.APPROVE_SETTLEMENT,
            ClaimDecisionAction.DECLINE,
        }

    def test_the_summary_names_the_actor(self) -> None:
        summary = lifecycle.decision_summary(
            ClaimDecisionAction.APPROVE_SETTLEMENT, actor="R. Marsh"
        )
        assert summary == "R. Marsh approved the settlement."


# ---------------------------------------------------------------------------
# What blocks a settlement
# ---------------------------------------------------------------------------


class TestApprovalBlocks:
    def test_a_clean_assigned_claim_within_authority_is_clear(self) -> None:
        blocks = lifecycle.approval_blocks(
            make_claim(reserve_minor=1_000_00),
            assignment=assigned(),
            authority_limit_minor=5_000_00,
        )
        assert blocks == []

    def test_a_decided_claim_reports_that_and_nothing_else(self) -> None:
        """The bug: an approved claim reported no blocks, so the bar stayed live.

        `decide` refuses a decided claim before it checks anything else, and this
        function is the list the screen renders — so zero blocks read as "go ahead"
        and the handler got a 409 from a cobalt button.

        Alone, because the other three are things to go and do. Telling somebody to
        clear a fraud flag on a claim that closed last week is noise.
        """
        blocks = lifecycle.approval_blocks(
            make_claim(status=ClaimStatus.APPROVED, fraud_flag=True),
            assignment=None,
            authority_limit_minor=None,
        )
        assert codes(blocks) == {BlockCode.DECIDED}
        assert "already been approved" in blocks[0].reason

    def test_a_declined_claim_says_declined(self) -> None:
        blocks = lifecycle.approval_blocks(
            make_claim(status=ClaimStatus.REJECTED), assignment=assigned()
        )
        assert codes(blocks) == {BlockCode.DECIDED}
        assert "rejected" in blocks[0].reason

    def test_every_verb_is_refused_on_a_decided_claim(self) -> None:
        """Including the two that are otherwise always available.

        Requesting information on a closed claim fails as surely as approving it
        does, because `decide` checks `is_terminal` first. A list that said
        otherwise would point a screen at a button the endpoint will not honour.
        """
        for decision in ClaimDecisionAction:
            blocks = lifecycle.blocks_for_decision(
                decision, make_claim(status=ClaimStatus.APPROVED), assignment=assigned()
            )
            assert codes(blocks) == {BlockCode.DECIDED}, decision

    def test_an_open_claim_still_lets_the_soft_verbs_through(self) -> None:
        """The property the change above must not have broken."""
        blocked = make_claim(reserve_minor=60_000_00)
        assert (
            lifecycle.blocks_for_decision(
                ClaimDecisionAction.REQUEST_INFORMATION,
                blocked,
                assignment=assigned(),
                authority_limit_minor=50_000_00,
            )
            == []
        )
        assert (
            lifecycle.blocks_for_decision(
                ClaimDecisionAction.APPROVE_SETTLEMENT,
                blocked,
                assignment=assigned(),
                authority_limit_minor=50_000_00,
            )
            != []
        )

    def test_an_unassigned_claim_is_blocked(self) -> None:
        assert BlockCode.UNASSIGNED in codes(
            lifecycle.approval_blocks(make_claim(), assignment=None, authority_limit_minor=None)
        )

    def test_a_recommendation_is_not_an_assignment(self) -> None:
        """`RECOMMENDED` is a resting state, not a handler on the file."""
        assert BlockCode.UNASSIGNED in codes(
            lifecycle.approval_blocks(
                make_claim(), assignment=recommended(), authority_limit_minor=5_000_00
            )
        )

    def test_a_fraud_flag_blocks(self) -> None:
        blocks = lifecycle.approval_blocks(
            make_claim(fraud_flag=True), assignment=assigned(), authority_limit_minor=None
        )
        assert BlockCode.FRAUD_FLAG in codes(blocks)

    def test_a_reserve_over_the_limit_blocks(self) -> None:
        blocks = lifecycle.approval_blocks(
            make_claim(reserve_minor=60_000_00),
            assignment=assigned(),
            authority_limit_minor=50_000_00,
        )
        assert BlockCode.OVER_AUTHORITY in codes(blocks)

    def test_a_limit_is_ignored_when_nobody_is_on_the_file(self) -> None:
        """The safety property: an unassigned claim is not measured against a limit.

        Passing the limit of somebody who is not assigned must not be able to
        *clear* anything either — so the claim is still blocked, on assignment.
        """
        blocks = lifecycle.approval_blocks(
            make_claim(reserve_minor=60_000_00),
            assignment=recommended(),
            authority_limit_minor=50_000_00,
        )
        assert BlockCode.OVER_AUTHORITY not in codes(blocks)
        assert BlockCode.UNASSIGNED in codes(blocks)

    def test_the_ledger_beats_the_cached_reserve(self) -> None:
        """A claim reserved under its limit but incurred over it is still over it.

        The case this exists for: `claims.reserve_minor` is a cache, and the
        authority check is the one place trusting a stale cache lets a claim
        through a limit it is above.
        """
        blocks = lifecycle.approval_blocks(
            make_claim(reserve_minor=40_000_00),
            assignment=assigned(),
            authority_limit_minor=50_000_00,
            incurred_minor=55_000_00,
        )
        assert BlockCode.OVER_AUTHORITY in codes(blocks)

    def test_a_limit_in_another_currency_cannot_clear_a_settlement(self) -> None:
        """£400,000 of exposure must not pass a $500,000 authority unchecked.

        The bug this guards: both sides were compared as bare minor units, so
        40,000,000 GBP-minor read as under 50,000,000 USD-minor and a claim cleared
        an authority it may well have been above. The rate that would settle it is a
        fact this system does not hold, so the safe answer on the field that releases
        money is to refuse and let a manager look.
        """
        blocks = lifecycle.approval_blocks(
            make_claim(reserve_minor=400_000_00, currency="GBP"),
            assignment=assigned(),
            authority_limit_minor=500_000_00,
            authority_currency="USD",
        )
        assert BlockCode.OVER_AUTHORITY in codes(blocks)
        assert "GBP" in blocks[0].reason and "USD" in blocks[0].reason

    def test_matching_currencies_are_compared_as_before(self) -> None:
        assert (
            lifecycle.approval_blocks(
                make_claim(reserve_minor=1_000_00, currency="GBP"),
                assignment=assigned(),
                authority_limit_minor=5_000_00,
                authority_currency="GBP",
            )
            == []
        )

    def test_an_absent_currency_is_not_treated_as_a_mismatch(self) -> None:
        """Refusing every settlement on a null would be the worse failure."""
        assert (
            lifecycle.approval_blocks(
                make_claim(reserve_minor=1_000_00, currency="GBP"),
                assignment=assigned(),
                authority_limit_minor=5_000_00,
                authority_currency=None,
            )
            == []
        )

    def test_the_stored_over_authority_flag_is_honoured_on_its_own(self) -> None:
        """Triage sets the flag; a missing limit must not silently clear it."""
        blocks = lifecycle.approval_blocks(
            make_claim(over_authority=True),
            assignment=assigned(),
            authority_limit_minor=None,
        )
        assert BlockCode.OVER_AUTHORITY in codes(blocks)

    @pytest.mark.parametrize(
        "decision",
        [
            ClaimDecisionAction.REQUEST_INFORMATION,
            ClaimDecisionAction.REFER_TO_MANAGER,
            ClaimDecisionAction.SEND_TO_APPROVAL,
        ],
    )
    def test_a_non_committing_decision_is_never_blocked(
        self, decision: ClaimDecisionAction
    ) -> None:
        """Otherwise a fraud-flagged, unassigned claim could not be referred at all."""
        assert (
            lifecycle.blocks_for_decision(
                decision,
                make_claim(fraud_flag=True, reserve_minor=10_000_000_00),
                assignment=None,
                authority_limit_minor=1_00,
            )
            == []
        )

    @pytest.mark.parametrize(
        "decision",
        [ClaimDecisionAction.APPROVE_SETTLEMENT, ClaimDecisionAction.DECLINE],
    )
    def test_a_committing_decision_is_blocked(self, decision: ClaimDecisionAction) -> None:
        assert lifecycle.blocks_for_decision(
            decision, make_claim(fraud_flag=True), assignment=None, authority_limit_minor=None
        )


# ---------------------------------------------------------------------------
# The activity log
# ---------------------------------------------------------------------------


class TestActivityCategories:
    def test_the_money_events_land_on_the_money(self) -> None:
        assert lifecycle.activity_category(AuditEventType.RESERVE_MOVED) is (
            ActivityCategory.FINANCIAL
        )
        assert lifecycle.activity_category(AuditEventType.PAYMENT_RECORDED) is (
            ActivityCategory.FINANCIAL
        )

    def test_a_recovery_is_its_own_category(self) -> None:
        assert lifecycle.activity_category(AuditEventType.RECOVERY_RECORDED) is (
            ActivityCategory.RECOVERY
        )

    def test_both_note_events_land_on_notes(self) -> None:
        """The notice's notes and the claim's notes read as one chronology."""
        assert lifecycle.activity_category(AuditEventType.NOTE_ADDED) is ActivityCategory.NOTE
        assert lifecycle.activity_category(AuditEventType.CLAIM_NOTE_ADDED) is (
            ActivityCategory.NOTE
        )

    def test_the_visit_events_land_on_the_inspection(self) -> None:
        """All six, and none of them on assessment.

        The `claim.*` fallback would have put every one of them under assessment
        and left the log's own inspection chip permanently empty, which is what
        happened until they were mapped.
        """
        for event_type in AuditEventType:
            if str(event_type).startswith("claim.inspection"):
                assert lifecycle.activity_category(event_type) is ActivityCategory.INSPECTION

    def test_every_known_event_type_is_categorised(self) -> None:
        """The table is exhaustive over the enum, and this is what keeps it so."""
        for event_type in AuditEventType:
            assert isinstance(lifecycle.activity_category(event_type), ActivityCategory)

    def test_no_category_is_unreachable(self) -> None:
        """Every chip on the log has something that can land under it.

        The assertion above cannot catch a category nobody maps to, because the
        prefix fallback answers for everything — which is exactly how the six
        inspection events sat under assessment while the inspection chip filtered
        to nothing. A chip that can never light is a lie about the record.
        """
        reached = {lifecycle.activity_category(event_type) for event_type in AuditEventType}
        assert reached == set(ActivityCategory), (
            f"nothing maps to {sorted(str(c) for c in set(ActivityCategory) - reached)}"
        )

    def test_an_uncatalogued_event_falls_back_on_its_prefix(self) -> None:
        assert lifecycle.activity_category("fnol.something_new") is ActivityCategory.INTAKE
        assert lifecycle.activity_category("claim.something_new") is ActivityCategory.ASSESSMENT

    def test_material_events_are_a_minority(self) -> None:
        """A log where every line is highlighted is a log nobody scans."""
        assert lifecycle.is_material(AuditEventType.CLAIM_DECIDED)
        assert not lifecycle.is_material(AuditEventType.PIPELINE_STARTED)
        assert len(lifecycle.MATERIAL_EVENTS) < len(list(AuditEventType)) / 2


# ---------------------------------------------------------------------------
# The ledger's arithmetic
# ---------------------------------------------------------------------------


class TestLedgerArithmetic:
    def test_held_is_the_sum_of_the_deltas(self) -> None:
        movements = [
            movement(MovementType.INDEMNITY, -500_00),
            movement(MovementType.INDEMNITY, 2_000_00),
            movement(MovementType.INDEMNITY, 40_000_00),
        ]
        assert lifecycle.held_by_movement_type(movements) == {"indemnity": 41_500_00}

    def test_heads_are_summed_separately(self) -> None:
        movements = [
            movement(MovementType.EXPENSE, 1_200_00),
            movement(MovementType.INDEMNITY, 40_000_00),
        ]
        assert lifecycle.held_by_movement_type(movements) == {
            "expense": 1_200_00,
            "indemnity": 40_000_00,
        }

    def test_an_empty_ledger_holds_nothing(self) -> None:
        assert lifecycle.held_by_movement_type([]) == {}

    def test_previous_held_excludes_the_newest_movement(self) -> None:
        """Movements arrive newest-first, which is what the [1:] slice relies on."""
        movements = [
            movement(MovementType.INDEMNITY, 20_000_00),
            movement(MovementType.INDEMNITY, 40_000_00),
        ]
        assert lifecycle.previous_held(movements, movement_type="indemnity") == 40_000_00

    def test_previous_held_is_zero_for_a_first_movement(self) -> None:
        movements = [movement(MovementType.INDEMNITY, 40_000_00)]
        assert lifecycle.previous_held(movements, movement_type="indemnity") == 0

    def test_previous_held_ignores_other_heads(self) -> None:
        movements = [
            movement(MovementType.EXPENSE, 900_00),
            movement(MovementType.INDEMNITY, 20_000_00),
            movement(MovementType.INDEMNITY, 40_000_00),
        ]
        assert lifecycle.previous_held(movements, movement_type="indemnity") == 40_000_00

    def test_previous_held_is_zero_for_an_untouched_head(self) -> None:
        movements = [movement(MovementType.INDEMNITY, 40_000_00)]
        assert lifecycle.previous_held(movements, movement_type="legal") == 0

    def test_incurred_adds_the_three_cost_heads(self) -> None:
        movements = [
            movement(MovementType.INDEMNITY, 40_000_00),
            movement(MovementType.EXPENSE, 1_200_00),
            movement(MovementType.LEGAL, 3_000_00),
        ]
        assert lifecycle.incurred_minor(movements) == 44_200_00

    def test_incurred_excludes_recoveries(self) -> None:
        """A recovery reduces the net and must not reduce what an authority sees.

        This is the case the split exists for: netting an identified-but-unbanked
        recovery off the incurred figure is how a claim slips under a limit it is
        genuinely over.
        """
        movements = [
            movement(MovementType.INDEMNITY, 60_000_00),
            movement(MovementType.RECOVERY, 20_000_00),
        ]
        assert lifecycle.incurred_minor(movements) == 60_000_00
