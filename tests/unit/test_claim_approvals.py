"""The manager's approval queue: the rows, the tiles, and one sheet.

This board read fixtures for as long as the workbench existed, so a claim a handler
escalated appeared on no manager's screen. The assertions worth reading are the ones
about that seam — an escalated claim turns up, an unassigned one is not hidden, and
the tiles cannot label a mixed-currency sum with one currency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import NotFoundError
from app.domain.enums import AssignmentStatus, ClaimStatus, NoteSection
from app.services.claims.approvals import ClaimApprovalService

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def claim(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "CLM-2026-000001",
        "status": str(ClaimStatus.ESCALATED),
        "claimant_name": "Northline Logistics Limited",
        "insured_name": "Northline Logistics Limited",
        "loss_location": "Thames Valley Park, Reading RG6 1PT",
        "loss_type": "escape_of_water",
        "currency": "GBP",
        "reserve_minor": 940_000_00,
        "priority": "high",
        "fraud_flag": False,
        "over_authority": True,
        "closed_at": None,
        "fnol_case_id": None,
        "updated_at": NOW - timedelta(days=6),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def assignment(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "handler_id": uuid.uuid4(),
        "handler_name": "Daniel Okafor",
        "status": str(AssignmentStatus.ASSIGNED),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def handler(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "full_name": "Daniel Okafor",
        "team": "Major Loss",
        "subject": "kc-okafor",
        "authority_limit_minor": 250_000_00,
        "currency": "GBP",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeClaimRepo:
    def __init__(
        self,
        rows: list[tuple[Any, Any]] | None = None,
        *,
        approved: list[Any] | None = None,
        notes: list[Any] | None = None,
        coverages: list[Any] | None = None,
        deductibles: list[Any] | None = None,
        recoveries: list[Any] | None = None,
    ) -> None:
        self.rows = rows or []
        self._approved = approved or []
        self._notes = notes or []
        self._coverages = coverages or []
        self._deductibles = deductibles or []
        self._recoveries = recoveries or []

    async def escalated_claims(self) -> list[tuple[Any, Any]]:
        return self.rows

    async def approved_since(self, moment: datetime) -> list[Any]:
        del moment
        return self._approved

    async def get_by_reference(self, reference: str) -> Any | None:
        return next((row[0] for row in self.rows if row[0].reference == reference), None)

    async def get_assignment(self, claim_id: uuid.UUID) -> Any | None:
        return next((row[1] for row in self.rows if row[0].id == claim_id), None)

    async def list_movements(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return []

    async def list_notes(self, claim_id: uuid.UUID, *, section: str | None = None) -> list[Any]:
        del claim_id, section
        return list(self._notes)

    async def list_coverages(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._coverages)

    async def list_deductibles(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._deductibles)

    async def list_recoveries(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self._recoveries)

    #: The two reads `app.services.claims.fraud_gate` makes. Empty on this fake:
    #: nothing here is testing the fraud review, and returning nothing is the state
    #: a claim with no indicators and no SIU case is in — so these specs go on
    #: measuring the authority and coverage rules they were written for.
    async def list_fraud_dispositions(self, claim_id: Any) -> list[Any]:
        del claim_id
        return []

    async def get_siu_case(self, claim_id: Any) -> Any | None:
        del claim_id
        return None


class FakeCaseRepo:
    def __init__(self, analysis: Any = None) -> None:
        self._analysis = analysis

    async def get(self, case_id: Any) -> Any | None:
        del case_id
        return SimpleNamespace(id=uuid.uuid4()) if self._analysis else None

    async def get_analysis(self, case_id: Any, kind: Any) -> Any | None:
        del case_id, kind
        return self._analysis


class FakeHandlerRepo:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    async def get(self, handler_id: Any) -> Any | None:
        return next((row for row in self.rows if row.id == handler_id), None)

    async def get_by_subject(self, subject: str) -> Any | None:
        return next((row for row in self.rows if row.subject == subject), None)

    async def list_available(self) -> list[Any]:
        return list(self.rows)


def build(
    rows: list[tuple[Any, Any]] | None = None,
    *,
    handlers: list[Any] | None = None,
    analysis: Any = None,
    approved: list[Any] | None = None,
    notes: list[Any] | None = None,
    coverages: list[Any] | None = None,
    deductibles: list[Any] | None = None,
    recoveries: list[Any] | None = None,
) -> ClaimApprovalService:
    return ClaimApprovalService(
        FakeClaimRepo(  # type: ignore[arg-type]
            rows,
            approved=approved,
            notes=notes,
            coverages=coverages,
            deductibles=deductibles,
            recoveries=recoveries,
        ),
        FakeCaseRepo(analysis),  # type: ignore[arg-type]
        FakeHandlerRepo(handlers),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


class TestQueue:
    @pytest.mark.asyncio
    async def test_an_escalated_claim_turns_up(self) -> None:
        """The whole point. This board read fixtures, so referrals arrived nowhere."""
        subject = claim()
        result = await build([(subject, assignment())]).queue(now=NOW)

        assert [row.reference for row in result.items] == ["CLM-2026-000001"]
        assert result.items[0].handler == "Daniel Okafor"
        assert result.items[0].waiting_days == 6

    @pytest.mark.asyncio
    async def test_an_unassigned_escalation_is_not_hidden(self) -> None:
        """It belongs to nobody's team and needs a decision most of all."""
        result = await build([(claim(), None)]).queue(now=NOW)

        assert result.items[0].handler == "Unassigned"

    @pytest.mark.asyncio
    async def test_the_reason_is_read_from_the_two_flags(self) -> None:
        both = claim(fraud_flag=True, over_authority=True)
        authority = claim(reference="CLM-2", fraud_flag=False, over_authority=True)
        neither = claim(reference="CLM-3", fraud_flag=False, over_authority=False)

        result = await build(
            [(both, assignment()), (authority, assignment()), (neither, assignment())]
        ).queue(now=NOW)

        assert [row.reason for row in result.items] == [
            "fraud_and_authority",
            "over_authority",
            "settlement",
        ]

    @pytest.mark.asyncio
    async def test_the_chips_narrow_by_what_is_blocking(self) -> None:
        """Not by status — every row here is escalated, so that would be ones."""
        flagged = claim(fraud_flag=True, over_authority=False)
        over = claim(reference="CLM-2", fraud_flag=False, over_authority=True)
        rows = [(flagged, assignment()), (over, assignment())]

        fraud = await build(rows).queue(chip="fraud_referral", now=NOW)
        authority = await build(rows).queue(chip="over_authority", now=NOW)

        assert [row.reference for row in fraud.items] == ["CLM-2026-000001"]
        assert [row.reference for row in authority.items] == ["CLM-2"]

    @pytest.mark.asyncio
    async def test_an_unknown_chip_empties_the_list(self) -> None:
        """A typo in a query string should not quietly widen it."""
        result = await build([(claim(), assignment())]).queue(chip="nonsense", now=NOW)
        assert result.items == []

    @pytest.mark.asyncio
    async def test_the_facet_counts_describe_the_same_read_as_the_list(self) -> None:
        rows = [
            (claim(fraud_flag=True, over_authority=True), assignment()),
            (claim(reference="CLM-2", fraud_flag=False, over_authority=True), assignment()),
        ]
        result = await build(rows).queue(chip="over_authority", now=NOW)
        counts = {facet.id: facet.count for facet in result.facets}

        assert counts["over_authority"] == 2
        assert counts["fraud_referral"] == 1
        assert len(result.items) == counts["over_authority"]

    @pytest.mark.asyncio
    async def test_an_empty_queue_says_nothing_is_waiting(self) -> None:
        result = await build([]).queue(now=NOW)
        assert result.clearance_note == "Nothing is waiting on you."


class TestScope:
    @pytest.mark.asyncio
    async def test_the_whole_book_is_reported_as_the_whole_book(self) -> None:
        """A queue that widened while calling itself *your decision* would lie."""
        result = await build([(claim(), assignment())]).queue(now=NOW)

        assert result.whole_book is True
        assert "cannot yet be narrowed" in result.description

    @pytest.mark.asyncio
    async def test_a_manager_with_a_team_gets_their_own_line(self) -> None:
        okafor = handler(subject="kc-okafor", team="Major Loss")
        marsh = handler(subject="kc-marsh", team="Property", full_name="Rebecca Marsh")
        mine = claim()
        theirs = claim(reference="CLM-2")

        service = build(
            [
                (mine, assignment(handler_id=okafor.id)),
                (theirs, assignment(handler_id=marsh.id)),
            ],
            handlers=[okafor, marsh],
        )
        result = await service.queue(subject="kc-okafor", now=NOW)

        assert [row.reference for row in result.items] == ["CLM-2026-000001"]
        assert result.whole_book is False
        assert "awaiting your decision" in result.description

    @pytest.mark.asyncio
    async def test_a_manager_not_in_the_directory_gets_the_whole_book(self) -> None:
        service = build([(claim(), assignment())], handlers=[])
        result = await service.queue(subject="kc-nobody", now=NOW)

        assert result.whole_book is True

    @pytest.mark.asyncio
    async def test_an_unassigned_escalation_appears_under_every_scope(self) -> None:
        okafor = handler(subject="kc-okafor")
        service = build([(claim(), None)], handlers=[okafor])
        result = await service.queue(subject="kc-okafor", now=NOW)

        assert [row.handler for row in result.items] == ["Unassigned"]


class TestTiles:
    @pytest.mark.asyncio
    async def test_a_single_currency_queue_totals_in_that_currency(self) -> None:
        rows = [(claim(currency="GBP", reserve_minor=940_000_00), assignment())]
        result = await build(rows).queue(now=NOW)
        tiles = {tile.id: tile.display for tile in result.metrics}

        assert tiles["total_reserve"] == "£940,000"
        assert tiles["awaiting_decision"] == "1"
        assert tiles["oldest_waiting"] == "6d"

    @pytest.mark.asyncio
    async def test_a_mixed_currency_queue_counts_rather_than_sums(self) -> None:
        """The third time this has been needed. Adding minor units across currencies
        produces a number that is not money, and labelling it with whichever came
        first turns a wrong number into a convincing one.
        """
        rows = [
            (claim(currency="GBP", reserve_minor=940_000_00), assignment()),
            (claim(reference="CLM-2", currency="USD", reserve_minor=3_700_000_00), assignment()),
        ]
        result = await build(rows).queue(now=NOW)
        tiles = {tile.id: tile.display for tile in result.metrics}

        assert tiles["total_reserve"] == "2 claims"

    @pytest.mark.asyncio
    async def test_approved_month_to_date_is_labelled_in_its_own_currency(self) -> None:
        """It reads outside the queue, so it need not match the queue's currency."""
        rows = [(claim(currency="GBP"), assignment())]
        approved = [claim(reference="CLM-9", currency="USD", reserve_minor=3_700_000_00)]
        result = await build(rows, approved=approved).queue(now=NOW)
        tiles = {tile.id: tile.display for tile in result.metrics}

        assert tiles["approved_mtd"] == "$3.7m"

    @pytest.mark.asyncio
    async def test_an_empty_queue_shows_a_dash_rather_than_nought_days(self) -> None:
        result = await build([]).queue(now=NOW)
        tiles = {tile.id: tile.display for tile in result.metrics}
        assert tiles["oldest_waiting"] == "—"


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


class TestSheet:
    @pytest.mark.asyncio
    async def test_the_settlement_nets_the_excess_and_the_recoveries(self) -> None:
        subject = claim(reserve_minor=940_000_00)
        service = build(
            [(subject, assignment())],
            deductibles=[SimpleNamespace(amount_minor=25_000_00)],
            recoveries=[
                SimpleNamespace(
                    status="pursuing",
                    expected_minor=150_000_00,
                    recovered_minor=30_000_00,
                    currency="GBP",
                )
            ],
        )
        sheet = await service.sheet("CLM-2026-000001", now=NOW)
        money = sheet.settlement

        assert money.requested.amount_minor == 940_000_00
        assert money.excess.amount_minor == 25_000_00
        #: What is **still** outstanding, not the gross expectation.
        assert money.recoverable.amount_minor == 120_000_00
        assert money.net_cost.amount_minor == 940_000_00 - 25_000_00 - 120_000_00

    @pytest.mark.asyncio
    async def test_the_net_cost_never_goes_below_nothing(self) -> None:
        """A claim whose recoveries exceed its reserve is a curiosity, not a credit."""
        subject = claim(reserve_minor=10_000_00)
        service = build(
            [(subject, assignment())],
            recoveries=[
                SimpleNamespace(
                    status="pursuing",
                    expected_minor=90_000_00,
                    recovered_minor=0,
                    currency="GBP",
                )
            ],
        )
        sheet = await service.sheet("CLM-2026-000001", now=NOW)
        assert sheet.settlement.net_cost.amount_minor == 0

    @pytest.mark.asyncio
    async def test_the_conditions_list_the_cleared_ones_too(self) -> None:
        """A list of only problems cannot tell a checked claim from an unchecked one."""
        service = build([(claim(over_authority=True, fraud_flag=False), assignment())])
        sheet = await service.sheet("CLM-2026-000001", now=NOW)
        states = {condition.id: condition.state for condition in sheet.conditions}

        assert states["over_authority"] == "blocking"
        assert states["fraud_flag"] == "cleared"
        #: Not listed at all on a live claim — it would read as a warning that the
        #: claim might already be closed.
        assert "decided" not in states

    @pytest.mark.asyncio
    async def test_the_unassigned_condition_offers_the_dialog_that_clears_it(self) -> None:
        service = build([(claim(over_authority=False), None)])
        sheet = await service.sheet("CLM-2026-000001", now=NOW)
        unassigned = next(c for c in sheet.conditions if c.id == "unassigned")

        assert unassigned.state == "blocking"
        assert [action.id for action in unassigned.actions] == ["assign_handler"]

    @pytest.mark.asyncio
    async def test_the_authority_limit_is_the_readers_own(self) -> None:
        """So the sheet names the figure this is measured against."""
        manager = handler(subject="kc-manager", authority_limit_minor=5_000_000_00)
        service = build([(claim(), assignment())], handlers=[manager])
        sheet = await service.sheet("CLM-2026-000001", subject="kc-manager", now=NOW)

        assert sheet.authority_limit is not None
        assert sheet.authority_limit.amount_minor == 5_000_000_00

    @pytest.mark.asyncio
    async def test_a_reader_with_no_authority_gets_null_rather_than_nought(self) -> None:
        """A newly registered account has none until somebody decides."""
        manager = handler(subject="kc-new", authority_limit_minor=None)
        service = build([(claim(), assignment())], handlers=[manager])
        sheet = await service.sheet("CLM-2026-000001", subject="kc-new", now=NOW)

        assert sheet.authority_limit is None

    @pytest.mark.asyncio
    async def test_the_coverage_verdict_comes_from_the_stored_analysis(self) -> None:
        analysis = SimpleNamespace(
            result={"indicator": "likely_covered", "reasoning": "The policy names escape of water."}
        )
        service = build([(claim(fnol_case_id=uuid.uuid4()), assignment())], analysis=analysis)
        sheet = await service.sheet("CLM-2026-000001", now=NOW)

        assert sheet.coverage is not None
        assert sheet.coverage.verdict == "covered"
        assert "escape of water" in sheet.coverage.summary

    @pytest.mark.asyncio
    async def test_no_analysis_means_no_verdict_rather_than_undetermined(self) -> None:
        service = build([(claim(), assignment())])
        sheet = await service.sheet("CLM-2026-000001", now=NOW)
        assert sheet.coverage is None

    @pytest.mark.asyncio
    async def test_the_handler_note_carries_computed_initials(self) -> None:
        """Slicing a name on the client breaks on a great many of them."""
        note = SimpleNamespace(
            author="Rebecca Marsh-Okonjo",
            created_at=NOW,
            body="Over my limit, so referring.",
            section=NoteSection.ASSESSMENT,
        )
        service = build([(claim(), assignment())], notes=[note])
        sheet = await service.sheet("CLM-2026-000001", now=NOW)

        assert sheet.handler_note is not None
        assert sheet.handler_note.initials == "RO"
        assert sheet.handler_note.author == "Rebecca Marsh-Okonjo"

    @pytest.mark.asyncio
    async def test_a_claim_with_no_note_reports_none(self) -> None:
        service = build([(claim(), assignment())])
        sheet = await service.sheet("CLM-2026-000001", now=NOW)
        assert sheet.handler_note is None

    @pytest.mark.asyncio
    async def test_an_unknown_reference_is_not_found(self) -> None:
        service = build([])
        with pytest.raises(NotFoundError):
            await service.sheet("CLM-9999", now=NOW)
