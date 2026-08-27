"""The adjuster's board: the chips, the tiles, one report, and filing it.

The distinction this whole file turns on is between the state of a *visit* and a
fact about a *report*. `sent_back` and `filed` are the second kind, and the reason
they are not statuses is testable: an inspection returned last week and being
worked again today has to be `in_progress` **and** sent back at once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.domain import inspection as rules
from app.domain.enums import AuditEventType, DamageSeverity, InspectionStatus
from app.services.claims.inspection_queue import ClaimInspectionQueueService

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def inspection(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "status": str(InspectionStatus.IN_PROGRESS),
        "reference": "INSP-2026-004412",
        "adjuster_name": "H. Okonjo",
        "adjuster_firm": "Crawford & Co",
        "commissioned_by": "R. Achebe",
        "commissioned_at": NOW - timedelta(days=10),
        "scheduled_at": NOW - timedelta(days=3),
        "attended_at": NOW - timedelta(days=3),
        "report_due_at": NOW + timedelta(days=7),
        "filed_at": None,
        "returned_at": None,
        "site_kind": "Warehouse",
        "site_address": "Unit 4, Trafford Park",
        "site_identifier": "Unit 4",
        "site_contact_name": "J. Bhatt",
        "site_contact_phone": "0161 555 0100",
        "site_access_note": "Gate code 4417.",
        "summary": "Racking deformed by heat.",
        "photographs": 41,
        "measurements": 6,
        "statements": 3,
        "documents": 2,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def claim(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "CLM-2026-000009",
        "claimant_name": "Harborline Cold Storage",
        "insured_name": "Harborline Cold Storage & Logistics",
        "loss_description": "Refrigeration failure",
        "loss_location": "2870 Patapsco Industrial Parkway",
        "loss_type": "machinery breakdown",
        "date_of_loss": NOW - timedelta(days=20),
        "currency": "GBP",
        "priority": "high",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def observation(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "element": "Racking, bays 1-14",
        "severity": str(DamageSeverity.SEVERE),
        "finding": "Deformed by heat.",
        "quantified_minor": 180_000_00,
        "currency": "GBP",
        "photo_count": 12,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def action(done: bool = False, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "label": "Provide the stock schedule",
        "owner": "Insured - J. Bhatt",
        "due_at": NOW + timedelta(days=3),
        "done": done,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeClaimRepo:
    def __init__(
        self,
        rows: list[tuple[Any, Any, int, str | None, int]] | None = None,
        *,
        observations: list[Any] | None = None,
        actions: list[Any] | None = None,
    ) -> None:
        self.rows = rows or []
        self._observations = observations or []
        self._actions = actions or []
        self.flushes = 0
        self.asked_for: str | None = None

    async def inspection_queue(
        self,
        *,
        adjuster_subject: str | None = None,
        adjuster_email: str | None = None,
    ) -> list[tuple[Any, Any, int, str | None, int]]:
        #: Both, because the service narrows on identity rather than on a display
        #: name and either column can carry it — the subject once the adjuster has
        #: recorded something, the address before that.
        self.asked_for = (adjuster_subject, adjuster_email)
        return self.rows

    async def inspection_by_claim_reference(self, reference: str) -> tuple[Any, Any] | None:
        return next(((row[0], row[1]) for row in self.rows if row[1].reference == reference), None)

    async def list_observations(self, inspection_id: uuid.UUID) -> list[Any]:
        del inspection_id
        return list(self._observations)

    async def list_inspection_actions(self, inspection_id: uuid.UUID) -> list[Any]:
        del inspection_id
        return list(self._actions)

    async def flush(self) -> None:
        self.flushes += 1


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def claim(self, subject: Any, *, event_type: Any, summary: str, actor: str, **rest: Any) -> Any:
        del subject
        event = {"event_type": str(event_type), "summary": summary, "actor": actor, **rest}
        self.events.append(event)
        return event


def build(
    rows: list[tuple[Any, Any, int, str | None, int]] | None = None,
    *,
    observations: list[Any] | None = None,
    actions: list[Any] | None = None,
) -> tuple[ClaimInspectionQueueService, FakeClaimRepo, RecordingAudit]:
    repo = FakeClaimRepo(rows, observations=observations, actions=actions)
    audit = RecordingAudit()
    return (
        ClaimInspectionQueueService(repo, audit),  # type: ignore[arg-type]
        repo,
        audit,
    )


def row(
    insp: Any = None,
    subject: Any = None,
    priced: int = 0,
    currency: str | None = "GBP",
) -> tuple[Any, Any, int, str | None, int]:
    """One queue row as the repository returns it: the sum **and its currency**."""
    return (insp or inspection(), subject or claim(), priced, currency if priced else None, 0)


# ---------------------------------------------------------------------------
# The chips
# ---------------------------------------------------------------------------


class TestChips:
    @pytest.mark.asyncio
    async def test_a_returned_report_is_in_progress_and_sent_back_at_once(self) -> None:
        """The combination a fifth status could not express.

        This is why `sent_back` is a column and not a state. Were it a status, an
        adjuster resuming work would clear the flag — which is exactly the moment a
        supervisor wants to see it.
        """
        resumed = inspection(status=str(InspectionStatus.IN_PROGRESS), returned_at=NOW)
        service, _, _ = build([row(resumed)])

        under_sent_back = await service.queue(chip="sent_back", now=NOW)
        under_booked = await service.queue(chip="booked_in_progress", now=NOW)

        assert [item.reference for item in under_sent_back.items] == ["CLM-2026-000009"]
        assert [item.reference for item in under_booked.items] == ["CLM-2026-000009"]
        assert under_sent_back.items[0].status == str(InspectionStatus.IN_PROGRESS)
        assert under_sent_back.items[0].sent_back is True

    @pytest.mark.asyncio
    async def test_a_filed_report_leaves_the_to_do_list(self) -> None:
        service, _, _ = build([row(inspection(filed_at=NOW))])

        assert (await service.queue(chip="filed", now=NOW)).items != []
        assert (await service.queue(chip="to_do", now=NOW)).items == []

    @pytest.mark.asyncio
    async def test_a_report_sent_back_is_owed_again(self) -> None:
        """`filed_at` cleared, `returned_at` kept — both halves of one act."""
        returned = inspection(filed_at=None, returned_at=NOW - timedelta(days=1))
        service, _, _ = build([row(returned)])

        assert (await service.queue(chip="to_do", now=NOW)).items != []
        assert (await service.queue(chip="filed", now=NOW)).items == []
        assert (await service.queue(chip="sent_back", now=NOW)).items != []

    @pytest.mark.asyncio
    async def test_the_counts_describe_the_same_read_as_the_list(self) -> None:
        """Six chip counts from one query, so a chip cannot disagree with the list."""
        service, _, _ = build(
            [
                row(inspection(status=str(InspectionStatus.TO_SCHEDULE))),
                row(inspection(status=str(InspectionStatus.IN_PROGRESS))),
                row(inspection(filed_at=NOW)),
            ]
        )
        result = await service.queue(chip="to_do", now=NOW)
        counts = {facet.id: facet.count for facet in result.facets}

        assert counts["all"] == 3
        assert counts["to_do"] == 2
        assert counts["filed"] == 1
        assert counts["to_schedule"] == 1
        assert result.total == counts["to_do"] == len(result.items)


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


class TestRows:
    @pytest.mark.asyncio
    async def test_nothing_priced_is_absent_rather_than_zero(self) -> None:
        """A loss that cost nothing and one nobody has costed are opposite facts."""
        service, _, _ = build([row(priced=0)])
        assert (await service.queue(chip="all", now=NOW)).items[0].quantified is None

    @pytest.mark.asyncio
    async def test_the_total_is_labelled_with_the_money_it_is_in(self) -> None:
        """Not with the claim's booking currency, which is the bug this replaced.

        A GBP schedule on a claim booked in dollars was reported as dollars: the
        same number, different money, and nothing on the screen to say so.
        """
        service, _, _ = build(
            [row(subject=claim(currency="USD"), priced=180_000_00, currency="GBP")]
        )
        quantified = (await service.queue(chip="all", now=NOW)).items[0].quantified

        assert quantified is not None
        assert quantified.amount_minor == 180_000_00
        assert quantified.currency == "GBP"

    @pytest.mark.asyncio
    async def test_the_site_falls_back_to_where_the_loss_happened(self) -> None:
        service, _, _ = build([row(inspection(site_address=None))])
        item = (await service.queue(chip="all", now=NOW)).items[0]
        assert item.site_address == "2870 Patapsco Industrial Parkway"

    @pytest.mark.asyncio
    async def test_an_undated_report_is_never_overdue(self) -> None:
        """A desk that promised nothing cannot have broken a promise."""
        service, _, _ = build([row(inspection(report_due_at=None))])
        item = (await service.queue(chip="all", now=NOW)).items[0]
        assert item.due_at is None
        assert item.overdue is False

    @pytest.mark.asyncio
    async def test_a_filed_report_is_not_late_even_past_its_date(self) -> None:
        late_but_filed = inspection(
            report_due_at=NOW - timedelta(days=8), filed_at=NOW - timedelta(days=9)
        )
        service, _, _ = build([row(late_but_filed)])
        assert (await service.queue(chip="all", now=NOW)).items[0].overdue is False

    @pytest.mark.asyncio
    async def test_the_footer_names_the_single_late_report(self) -> None:
        service, _, _ = build([row(inspection(report_due_at=NOW - timedelta(days=8)))])
        result = await service.queue(chip="all", now=NOW)
        assert result.alert_note is not None
        assert "CLM-2026-000009" in result.alert_note

    @pytest.mark.asyncio
    async def test_nothing_late_says_nothing(self) -> None:
        service, _, _ = build([row()])
        assert (await service.queue(chip="all", now=NOW)).alert_note is None


class TestWhoseBoardItIs:
    @pytest.mark.asyncio
    async def test_the_whole_desk_is_reported_as_the_whole_desk(self) -> None:
        """A board that looked personal while showing everybody would misrepresent it."""
        service, repo, _ = build([row()])
        result = await service.queue(chip="all", now=NOW)

        assert result.whole_desk is True
        assert repo.asked_for == (None, None)
        assert "not only your own" in result.description

    @pytest.mark.asyncio
    async def test_narrowing_to_one_adjuster_says_so_instead(self) -> None:
        service, repo, _ = build([row()])
        result = await service.queue(
            chip="all",
            adjuster_subject="kc-9f1",
            adjuster_email="h.okonjo@adjusters.example",
            now=NOW,
        )

        assert result.whole_desk is False
        assert repo.asked_for == ("kc-9f1", "h.okonjo@adjusters.example")
        assert "commissioned to you" in result.description

    @pytest.mark.asyncio
    async def test_an_address_alone_is_enough_to_narrow(self) -> None:
        """The first visit, before the adjuster has recorded anything.

        `adjuster_subject` is claimed on the first write, so an adjuster's very first
        visit is identified by the address the handler was given and nothing else. A
        board that needed the subject would show them an empty list on the one day it
        matters most.
        """
        service, repo, _ = build([row()])
        result = await service.queue(
            chip="all", adjuster_email="h.okonjo@adjusters.example", now=NOW
        )

        assert result.whole_desk is False
        assert repo.asked_for == (None, "h.okonjo@adjusters.example")


# ---------------------------------------------------------------------------
# One report
# ---------------------------------------------------------------------------


class TestReport:
    @pytest.mark.asyncio
    async def test_the_report_carries_what_the_visit_found(self) -> None:
        service, _, _ = build(
            [row()],
            observations=[observation(), observation(quantified_minor=None, currency=None)],
            actions=[action()],
        )
        report = await service.report("CLM-2026-000009", now=NOW)

        assert report.priced_count == 1
        assert report.quantified_total is not None
        assert report.quantified_total.amount_minor == 180_000_00
        assert report.quantified_total.currency == "GBP"
        assert len(report.observations) == 2
        assert report.observations[1].quantified is None
        assert len(report.actions) == 1

    @pytest.mark.asyncio
    async def test_the_reported_cause_is_what_the_adjuster_was_told(self) -> None:
        """Not their conclusion, which is the summary and the findings beneath it."""
        service, _, _ = build([row()])
        report = await service.report("CLM-2026-000009", now=NOW)
        assert report.visit.reported_cause == "machinery breakdown"

    @pytest.mark.asyncio
    async def test_the_gaps_are_named_rather_than_drawn_empty(self) -> None:
        service, _, _ = build([row()])
        report = await service.report("CLM-2026-000009", now=NOW)

        assert report.not_recorded
        assert any("liability" in line.lower() for line in report.not_recorded)
        assert any("recommendation" in line.lower() for line in report.not_recorded)

    @pytest.mark.asyncio
    async def test_a_schedule_in_two_currencies_reports_no_total(self) -> None:
        """There is no honest single figure, and this system holds no rate.

        Such a schedule cannot be written any more — the service refuses a currency
        the claim is not booked in — but a row predating that rule must not produce
        a total in whichever currency happened to sort first.
        """
        service, _, _ = build(
            [row()],
            observations=[
                observation(quantified_minor=100_00, currency="GBP"),
                observation(quantified_minor=100_00, currency="USD"),
            ],
        )
        report = await service.report("CLM-2026-000009", now=NOW)

        assert report.priced_count == 2
        assert report.quantified_total is None
        # The rows are still there. It is the total that cannot be stated.
        assert len(report.observations) == 2

    @pytest.mark.asyncio
    async def test_a_claim_with_no_visit_says_what_to_do(self) -> None:
        service, _, _ = build([])
        with pytest.raises(NotFoundError) as raised:
            await service.report("CLM-2026-000009", now=NOW)
        assert "commissioned" in str(raised.value)


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


class TestFilingBlockers:
    def test_an_unvisited_report_cannot_be_filed(self) -> None:
        blockers = rules.filing_blockers(inspection(attended_at=None), observations=[], actions=[])
        assert any("attended" in blocker for blocker in blockers)

    def test_an_unpriced_finding_is_still_a_finding(self) -> None:
        """Insisting on money here would push adjusters into inventing figures."""
        blockers = rules.filing_blockers(
            inspection(),
            observations=[observation(quantified_minor=None, currency=None)],
            actions=[],
        )
        assert blockers == []

    def test_an_open_action_blocks_and_is_counted(self) -> None:
        blockers = rules.filing_blockers(
            inspection(),
            observations=[observation()],
            actions=[action(), action(), action(done=True)],
        )
        assert blockers == ["2 actions are still open."]

    def test_one_open_action_reads_as_one(self) -> None:
        blockers = rules.filing_blockers(
            inspection(), observations=[observation()], actions=[action()]
        )
        assert blockers == ["1 action is still open."]


class TestFiling:
    @pytest.mark.asyncio
    async def test_filing_records_the_moment_and_audits_it(self) -> None:
        service, repo, audit = build(
            [row()], observations=[observation()], actions=[action(done=True)]
        )
        filed = await service.file_report("CLM-2026-000009", actor="H. Okonjo")

        assert filed.filed_at is not None
        assert repo.flushes == 1
        assert audit.events[-1]["event_type"] == str(AuditEventType.INSPECTION_FILED)
        assert "H. Okonjo" in audit.events[-1]["summary"]

    @pytest.mark.asyncio
    async def test_filing_does_not_move_the_visit(self) -> None:
        """Filing is a fact about the report. The visit is still whatever it was."""
        service, _, _ = build([row()], observations=[observation()])
        filed = await service.file_report("CLM-2026-000009", actor="H. Okonjo")
        assert filed.status == str(InspectionStatus.IN_PROGRESS)

    @pytest.mark.asyncio
    async def test_the_refusal_names_every_blocker(self) -> None:
        """An adjuster told only that it is "not ready" has to guess which of three."""
        service, _, _ = build([row(inspection(attended_at=None))])
        with pytest.raises(ConflictError) as raised:
            await service.file_report("CLM-2026-000009", actor="H. Okonjo")

        message = str(raised.value)
        assert "attended" in message
        assert "recorded against the risk" in message

    @pytest.mark.asyncio
    async def test_filing_twice_is_refused_rather_than_restamped(self) -> None:
        already = inspection(filed_at=NOW - timedelta(days=1))
        service, _, _ = build([row(already)], observations=[observation()])

        with pytest.raises(ConflictError) as raised:
            await service.file_report("CLM-2026-000009", actor="H. Okonjo")
        assert "already filed" in str(raised.value)
