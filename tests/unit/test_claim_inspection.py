"""The field inspection: the machine, the arithmetic, and the service that writes both.

Two halves, the same shape `test_claim_coverage.py` uses.

The first exercises `app.domain.inspection`, which is pure and takes plain
objects. The cases worth reading there are the ones where being wrong costs money
or misleads a handler: an unpriced observation counted as zero, an untouched
element counted as damage, a completed report read as a finished job.

The second exercises `ClaimInspectionService` against an in-memory repository
that keeps real state, because every assertion is about a *consequence* — the
visit could not be attended before it was booked, the site fell back to the
claim's loss location, the audit event named who commissioned it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.domain import inspection as rules
from app.domain.enums import (
    AuditEventType,
    ClaimStatus,
    DamageSeverity,
    InspectionStatus,
)
from app.services.claims.inspection import ClaimInspectionService

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def observation(
    severity: DamageSeverity | str = DamageSeverity.MODERATE,
    quantified_minor: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(severity=str(severity), quantified_minor=quantified_minor)


def action(done: bool = False) -> SimpleNamespace:
    return SimpleNamespace(done=done)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_a_visit_has_to_be_commissioned_before_it_can_be_booked(self) -> None:
        assert rules.can_transition(InspectionStatus.NOT_COMMISSIONED, InspectionStatus.TO_SCHEDULE)
        assert not rules.can_transition(
            InspectionStatus.NOT_COMMISSIONED, InspectionStatus.VISIT_BOOKED
        )

    def test_a_visit_cannot_be_attended_before_it_is_booked(self) -> None:
        """The edge that stops a schedule of damage existing on an unvisited claim."""
        assert not rules.can_transition(InspectionStatus.TO_SCHEDULE, InspectionStatus.IN_PROGRESS)
        assert rules.can_transition(InspectionStatus.VISIT_BOOKED, InspectionStatus.IN_PROGRESS)

    def test_a_booked_visit_can_go_back_to_being_unbooked(self) -> None:
        """Cancellations happen, and a cancelled visit must not stay booked.

        Without this edge the desk would either mark a missed visit attended or
        leave it booked for a date that has gone by, and both are worse than the
        state being unbooked.
        """
        assert rules.can_transition(InspectionStatus.VISIT_BOOKED, InspectionStatus.TO_SCHEDULE)

    def test_a_report_sent_back_can_be_visited_again(self) -> None:
        """The second visit — the normal outcome on a large loss, not an exception."""
        assert rules.can_transition(InspectionStatus.MORE_NEEDED, InspectionStatus.VISIT_BOOKED)

    def test_a_completed_inspection_goes_nowhere(self) -> None:
        assert rules.allowed_transitions(InspectionStatus.COMPLETED) == frozenset()

    def test_an_unknown_status_is_refused_rather_than_raising(self) -> None:
        """A row written by an older revision is a thing to refuse to move."""
        assert rules.can_transition("assigned_to_surveyor", InspectionStatus.COMPLETED) is False
        assert rules.allowed_transitions("assigned_to_surveyor") == frozenset()

    def test_findings_can_still_arrive_after_the_report_was_accepted(self) -> None:
        """A supplementary observation a week later is normal, not an error.

        Refusing it would push a real finding into a free-text note, which is the
        outcome the record exists to prevent.
        """
        assert rules.has_attended(InspectionStatus.COMPLETED)
        assert not rules.has_attended(InspectionStatus.VISIT_BOOKED)


# ---------------------------------------------------------------------------
# What the visit found
# ---------------------------------------------------------------------------


class TestQuantified:
    def test_an_unpriced_observation_is_not_a_zero(self) -> None:
        """The one that would make a partial schedule read as a complete one."""
        rows = [observation(quantified_minor=120_000_00), observation()]
        assert rules.quantified_minor(rows) == 120_000_00
        assert rules.priced_count(rows) == 1

    def test_an_untouched_element_costs_nothing_even_if_it_carries_a_figure(self) -> None:
        """Free-text-fed fields eventually carry one, and it must not be summed."""
        rows = [
            observation(DamageSeverity.SEVERE, 80_000_00),
            observation(DamageSeverity.UNAFFECTED, 5_000_00),
        ]
        assert rules.quantified_minor(rows) == 80_000_00

    def test_an_explicit_zero_is_kept_and_counted_as_priced(self) -> None:
        """Zero is an adjuster's answer; null is the absence of one."""
        rows = [observation(DamageSeverity.LIGHT, 0)]
        assert rules.quantified_minor(rows) == 0
        assert rules.priced_count(rows) == 1

    def test_the_total_carries_the_currency_the_rows_are_in(self) -> None:
        rows = [
            observation(DamageSeverity.SEVERE, 100_00),
            observation(DamageSeverity.LIGHT, 50_00),
        ]
        for row in rows:
            row.currency = "GBP"
        assert rules.quantified_total(rows) == (150_00, "GBP")

    def test_a_mixed_schedule_has_no_honest_total(self) -> None:
        """Converting would need a rate; picking one would misreport the other."""
        first = observation(DamageSeverity.SEVERE, 100_00)
        first.currency = "GBP"
        second = observation(DamageSeverity.SEVERE, 100_00)
        second.currency = "USD"
        assert rules.quantified_total([first, second]) is None

    def test_nothing_priced_has_no_total_either(self) -> None:
        assert rules.quantified_total([observation(DamageSeverity.SEVERE)]) is None
        assert rules.quantified_total([]) is None

    def test_material_damage_is_the_write_offs_and_the_severe(self) -> None:
        rows = [
            observation(DamageSeverity.TOTAL_LOSS),
            observation(DamageSeverity.SEVERE),
            observation(DamageSeverity.MODERATE),
            observation(DamageSeverity.LIGHT),
            observation(DamageSeverity.UNAFFECTED),
        ]
        assert rules.material_count(rows) == 2

    def test_nothing_observed_totals_to_nothing(self) -> None:
        assert rules.quantified_minor([]) == 0
        assert rules.priced_count([]) == 0
        assert rules.material_count([]) == 0


class TestOutstanding:
    def test_open_actions_are_counted_and_done_ones_are_not(self) -> None:
        assert rules.outstanding_actions([action(), action(done=True), action()]) == 2

    def test_a_completed_report_with_open_actions_is_not_settled(self) -> None:
        """`completed` says the report arrived, not that the job is finished."""
        assert rules.is_settled(InspectionStatus.COMPLETED, [action(done=True), action()]) is False

    def test_settled_needs_the_report_and_an_empty_list(self) -> None:
        assert rules.is_settled(InspectionStatus.COMPLETED, [action(done=True)]) is True
        assert rules.is_settled(InspectionStatus.COMPLETED, []) is True
        assert rules.is_settled(InspectionStatus.IN_PROGRESS, []) is False


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class FakeClaimRepo:
    """An in-memory repository that does **not** autoflush, because the real one does not.

    `app.db.session` builds its sessions with `autoflush=False`, so a row that has
    been `add`ed is invisible to the next `SELECT` until somebody flushes. A fake
    that made every write immediately readable would pass a service that never
    flushed and then fail in production the first time a request wrote a row and
    read it back in the same breath — which is exactly what happened: commissioning
    a visit and booking it in one request answered *"no inspection has been
    commissioned"* from the second half of its own transaction.

    So `add_*` puts the row in `_pending` and only `flush` makes it visible. The
    small amount of bookkeeping is the price of the fake being able to fail the way
    the database does.
    """

    def __init__(self) -> None:
        self.inspection: Any = None
        self.observations: list[Any] = []
        self.actions: list[Any] = []
        #: Added but not yet pushed. Invisible to every read below.
        self._pending: list[tuple[str, Any]] = []
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1
        for kind, row in self._pending:
            if kind == "inspection":
                self.inspection = row
            elif kind == "observation":
                self.observations.append(row)
            else:
                self.actions.append(row)
        self._pending.clear()

    async def get_inspection(self, claim_id: uuid.UUID) -> Any | None:
        del claim_id
        return self.inspection

    def add_inspection(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self._pending.append(("inspection", row))
        return row

    async def list_observations(self, inspection_id: uuid.UUID) -> list[Any]:
        del inspection_id
        return list(self.observations)

    def add_observation(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self._pending.append(("observation", row))
        return row

    async def list_inspection_actions(self, inspection_id: uuid.UUID) -> list[Any]:
        del inspection_id
        return list(self.actions)

    def add_inspection_action(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self._pending.append(("action", row))
        return row

    async def get_inspection_action(
        self, inspection_id: uuid.UUID, action_id: uuid.UUID
    ) -> Any | None:
        del inspection_id
        return next((row for row in self.actions if row.id == action_id), None)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def claim(self, claim: Any, *, event_type: Any, summary: str, actor: str, **rest: Any) -> Any:
        del claim
        event = {"event_type": str(event_type), "summary": summary, "actor": actor, **rest}
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        return [event["event_type"] for event in self.events]

    def summaries(self) -> list[str]:
        return [event["summary"] for event in self.events]


def make_claim(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "CLM-2026-000001",
        "status": ClaimStatus.IN_REVIEW,
        "currency": "GBP",
        "loss_location": "Unit 4, Trafford Park, Manchester M17 1AB",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build() -> tuple[ClaimInspectionService, FakeClaimRepo, RecordingAudit]:
    claims = FakeClaimRepo()
    audit = RecordingAudit()
    service = ClaimInspectionService(
        claims,  # type: ignore[arg-type]
        audit,  # type: ignore[arg-type]
    )
    return service, claims, audit


BOOKED = datetime(2026, 9, 3, 9, 30, tzinfo=UTC)
ATTENDED = datetime(2026, 9, 3, 11, 15, tzinfo=UTC)


async def commissioned(
    *, claim: Any = None
) -> tuple[ClaimInspectionService, FakeClaimRepo, RecordingAudit, Any]:
    """A claim with a visit instructed and nothing else. The common starting state."""
    service, claims, audit = build()
    subject = claim or make_claim()
    await service.commission(subject, actor="R. Achebe", adjuster_firm="Crawford & Co")
    return service, claims, audit, subject


class TestCommission:
    @pytest.mark.asyncio
    async def test_a_commissioned_visit_starts_unbooked(self) -> None:
        service, claims, audit, claim = await commissioned()
        del service

        assert claims.inspection.status == str(InspectionStatus.TO_SCHEDULE)
        assert claims.inspection.commissioned_by == "R. Achebe"
        assert claims.inspection.scheduled_at is None
        assert claims.inspection.attended_at is None
        assert audit.types() == [str(AuditEventType.INSPECTION_COMMISSIONED)]
        assert "Crawford & Co" in audit.summaries()[0]
        del claim

    @pytest.mark.asyncio
    async def test_the_site_falls_back_to_where_the_loss_happened(self) -> None:
        """An adjuster needs an address, and the claim already holds one.

        Making the desk retype it invites a typo on the single field the visit
        cannot proceed without.
        """
        _, claims, _, claim = await commissioned()
        assert claims.inspection.site_address == claim.loss_location

    @pytest.mark.asyncio
    async def test_a_stated_site_wins_over_the_loss_location(self) -> None:
        service, claims, _ = build()
        await service.commission(
            make_claim(),
            actor="R. Achebe",
            site_address="Bay 7, the north yard",
            site_access_note="Gate code 4417. Keys with the night manager.",
        )
        assert claims.inspection.site_address == "Bay 7, the north yard"
        assert "4417" in claims.inspection.site_access_note

    @pytest.mark.asyncio
    async def test_nothing_about_the_adjuster_is_invented(self) -> None:
        """A desk commissioning at four in the afternoon knows only that one is needed."""
        service, claims, _ = build()
        await service.commission(make_claim(), actor="R. Achebe")
        assert claims.inspection.adjuster_name is None
        assert claims.inspection.adjuster_firm is None
        assert claims.inspection.reference is None

    @pytest.mark.asyncio
    async def test_a_second_inspection_is_refused_and_says_what_to_do_instead(self) -> None:
        service, _, _, claim = await commissioned()
        with pytest.raises(ConflictError) as raised:
            await service.commission(claim, actor="R. Achebe")
        assert "further visit" in str(raised.value)

    @pytest.mark.asyncio
    async def test_commissioning_and_booking_in_one_request_works(self) -> None:
        """The one that was broken in the browser, and the reason the fake defers writes.

        Sessions are built with `autoflush=False`. Commissioning added the row and
        `schedule` looked for it with a `SELECT`, which did not see it — so a form
        that commissioned and booked in one act answered *"no inspection has been
        commissioned on this claim"* from the second half of its own transaction.
        """
        service, claims, _ = build()
        claim = make_claim()

        await service.commission(claim, actor="R. Achebe")
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")

        assert claims.inspection.status == str(InspectionStatus.VISIT_BOOKED)
        assert claims.inspection.scheduled_at == BOOKED

    @pytest.mark.asyncio
    async def test_a_decided_claim_is_not_one_to_send_an_adjuster_to(self) -> None:
        service, _, _ = build()
        with pytest.raises(ConflictError):
            await service.commission(make_claim(status=ClaimStatus.APPROVED), actor="R. Achebe")


class TestSchedule:
    @pytest.mark.asyncio
    async def test_booking_records_the_date_and_the_adjusters_own_reference(self) -> None:
        """Which is when they usually arrive — with the firm's acknowledgement."""
        service, claims, audit, claim = await commissioned()
        await service.schedule(
            claim,
            scheduled_at=BOOKED,
            actor="R. Achebe",
            adjuster_name="H. Okonjo",
            reference="INSP-2026-004412",
        )

        assert claims.inspection.status == str(InspectionStatus.VISIT_BOOKED)
        assert claims.inspection.scheduled_at == BOOKED
        assert claims.inspection.adjuster_name == "H. Okonjo"
        assert claims.inspection.reference == "INSP-2026-004412"
        assert audit.types()[-1] == str(AuditEventType.INSPECTION_SCHEDULED)
        assert "booked" in audit.summaries()[-1]

    @pytest.mark.asyncio
    async def test_moving_a_booked_visit_reads_as_a_move_not_a_first_booking(self) -> None:
        """The trail has to keep the difference; a desk chasing a slipped visit reads it."""
        service, claims, audit, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.schedule(claim, scheduled_at=BOOKED + timedelta(days=2), actor="R. Achebe")

        assert claims.inspection.scheduled_at == BOOKED + timedelta(days=2)
        assert "moved" in audit.summaries()[-1]
        assert audit.types().count(str(AuditEventType.INSPECTION_SCHEDULED)) == 2

    @pytest.mark.asyncio
    async def test_a_rebooking_does_not_wipe_the_adjuster(self) -> None:
        service, claims, _, claim = await commissioned()
        await service.schedule(
            claim, scheduled_at=BOOKED, actor="R. Achebe", adjuster_name="H. Okonjo"
        )
        await service.schedule(claim, scheduled_at=BOOKED + timedelta(days=2), actor="R. Achebe")
        assert claims.inspection.adjuster_name == "H. Okonjo"

    @pytest.mark.asyncio
    async def test_a_second_visit_can_be_booked_after_a_report_is_sent_back(self) -> None:
        service, claims, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        await service.set_status(
            claim,
            status=InspectionStatus.MORE_NEEDED,
            actor="R. Achebe",
            reason="The stock schedule is missing.",
        )

        await service.schedule(claim, scheduled_at=BOOKED + timedelta(days=14), actor="R. Achebe")
        assert claims.inspection.status == str(InspectionStatus.VISIT_BOOKED)

    @pytest.mark.asyncio
    async def test_booking_a_completed_inspection_is_refused(self) -> None:
        service, _, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        await service.set_status(claim, status=InspectionStatus.COMPLETED, actor="R. Achebe")

        with pytest.raises(ConflictError):
            await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")

    @pytest.mark.asyncio
    async def test_booking_a_visit_nobody_commissioned_says_so(self) -> None:
        service, _, _ = build()
        with pytest.raises(NotFoundError) as raised:
            await service.schedule(make_claim(), scheduled_at=BOOKED, actor="R. Achebe")
        assert "Commission one" in str(raised.value)


class TestAttendance:
    @pytest.mark.asyncio
    async def test_an_unbooked_visit_cannot_be_attended(self) -> None:
        """A missed visit is rebooked. It is never marked attended."""
        service, _, _, claim = await commissioned()
        with pytest.raises(ConflictError) as raised:
            await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        assert "Book the visit first" in str(raised.value)

    @pytest.mark.asyncio
    async def test_attendance_records_the_date_and_what_was_collected(self) -> None:
        service, claims, audit, claim = await commissioned()
        await service.schedule(
            claim, scheduled_at=BOOKED, actor="R. Achebe", adjuster_name="H. Okonjo"
        )
        await service.record_attendance(
            claim,
            attended_at=ATTENDED,
            actor="H. Okonjo",
            summary="Racking bays 1-14 deformed by heat. Roof deck sound.",
            photographs=41,
            measurements=6,
        )

        assert claims.inspection.status == str(InspectionStatus.IN_PROGRESS)
        assert claims.inspection.attended_at == ATTENDED
        assert claims.inspection.photographs == 41
        assert claims.inspection.measurements == 6
        assert audit.types()[-1] == str(AuditEventType.INSPECTION_ATTENDED)
        assert "H. Okonjo" in audit.summaries()[-1]

    @pytest.mark.asyncio
    async def test_an_omitted_count_is_left_alone_rather_than_zeroed(self) -> None:
        """Correcting the photographs must not silently wipe the statements."""
        service, claims, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(
            claim, attended_at=ATTENDED, actor="H. Okonjo", photographs=41, statements=3
        )
        await service.record_attendance(
            claim, attended_at=ATTENDED, actor="H. Okonjo", photographs=44
        )

        assert claims.inspection.photographs == 44
        assert claims.inspection.statements == 3

    @pytest.mark.asyncio
    async def test_a_corrected_count_is_not_a_second_visit(self) -> None:
        """The trail has to say which it was.

        Counts arrive late — the adjuster says forty-one on the telephone and the
        report shows forty-four — and a second "attended" event would read as the
        adjuster having gone twice.
        """
        service, _, audit, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        await service.record_attendance(
            claim, attended_at=ATTENDED, actor="R. Achebe", photographs=44
        )

        assert "attended on" in audit.summaries()[-2]
        assert "corrected" in audit.summaries()[-1]


class TestStatus:
    @pytest.mark.asyncio
    async def test_a_report_can_be_accepted(self) -> None:
        service, claims, audit, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        await service.set_status(claim, status=InspectionStatus.COMPLETED, actor="R. Achebe")

        assert claims.inspection.status == str(InspectionStatus.COMPLETED)
        assert audit.types()[-1] == str(AuditEventType.INSPECTION_STATUS_CHANGED)

    @pytest.mark.asyncio
    async def test_the_reason_for_sending_it_back_reaches_the_trail(self) -> None:
        service, _, audit, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        await service.set_status(
            claim,
            status=InspectionStatus.MORE_NEEDED,
            actor="R. Achebe",
            reason="No stock schedule for the mezzanine.",
        )
        assert "No stock schedule" in audit.summaries()[-1]
        assert audit.events[-1]["context"] == {"reason": "No stock schedule for the mezzanine."}

    @pytest.mark.asyncio
    async def test_an_illegal_move_names_the_move_rather_than_failing_quietly(self) -> None:
        service, _, _, claim = await commissioned()
        with pytest.raises(ConflictError) as raised:
            await service.set_status(claim, status=InspectionStatus.COMPLETED, actor="R. Achebe")
        assert "to schedule" in str(raised.value)
        assert "completed" in str(raised.value)

    @pytest.mark.asyncio
    async def test_moving_to_where_it_already_is_is_refused(self) -> None:
        """Not silently accepted: it would write an audit event saying nothing happened."""
        service, _, _, claim = await commissioned()
        with pytest.raises(ConflictError) as raised:
            await service.set_status(claim, status=InspectionStatus.TO_SCHEDULE, actor="R. Achebe")
        assert "already" in str(raised.value)


class TestObservations:
    @pytest.mark.asyncio
    async def test_nothing_can_be_observed_before_anybody_has_been(self) -> None:
        """A schedule of damage on an unvisited claim is somebody's guess."""
        service, _, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")

        with pytest.raises(ConflictError) as raised:
            await service.add_observation(
                claim,
                element="Racking, bays 1-14",
                severity=DamageSeverity.SEVERE,
                finding="Deformed by heat.",
                actor="H. Okonjo",
            )
        assert "attended" in str(raised.value)

    @pytest.mark.asyncio
    async def test_a_costed_finding_keeps_its_currency(self) -> None:
        service, claims, audit, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")

        await service.add_observation(
            claim,
            element="Racking, bays 1-14",
            severity=DamageSeverity.SEVERE,
            finding="Deformed by heat; replacement rather than repair.",
            actor="H. Okonjo",
            quantified_minor=180_000_00,
            currency="gbp",
            photo_count=12,
        )

        row = claims.observations[0]
        assert row.quantified_minor == 180_000_00
        assert row.currency == "GBP"
        assert row.photo_count == 12
        assert audit.types()[-1] == str(AuditEventType.INSPECTION_OBSERVED)

    @pytest.mark.asyncio
    async def test_an_unpriced_finding_is_recorded_as_unpriced(self) -> None:
        """What the adjuster left to a contractor's quote. Not zero."""
        service, claims, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")

        await service.add_observation(
            claim,
            element="Roof deck, north bay",
            severity=DamageSeverity.LIGHT,
            finding="Smoke staining; contractor to quote for cleaning.",
            actor="H. Okonjo",
        )
        assert claims.observations[0].quantified_minor is None
        assert claims.observations[0].currency is None

    @pytest.mark.asyncio
    async def test_an_amount_with_no_currency_is_not_an_amount(self) -> None:
        service, _, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")

        with pytest.raises(ValidationError):
            await service.add_observation(
                claim,
                element="Stock, mezzanine",
                severity=DamageSeverity.TOTAL_LOSS,
                finding="Written off.",
                actor="H. Okonjo",
                quantified_minor=42_000_00,
            )

    @pytest.mark.asyncio
    async def test_a_currency_the_claim_is_not_booked_in_is_refused(self) -> None:
        """Because the total would then be a figure in neither currency.

        The tab and the adjuster's board both sum this schedule. Two currencies in
        it make that sum either a fabrication or an FX conversion, and the rate that
        would do the second is a fact this system does not hold — the same reason
        the reserve ledger's accounting pair is nullable.
        """
        service, _, _, claim = await commissioned(claim=make_claim(currency="USD"))
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")

        with pytest.raises(ValidationError) as raised:
            await service.add_observation(
                claim,
                element="Racking, bays 1-14",
                severity=DamageSeverity.SEVERE,
                finding="Deformed by heat.",
                actor="H. Okonjo",
                quantified_minor=180_000_00,
                currency="GBP",
            )
        assert "booked in USD" in str(raised.value)

    @pytest.mark.asyncio
    async def test_a_supplementary_finding_survives_an_accepted_report(self) -> None:
        service, claims, _, claim = await commissioned()
        await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
        await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
        await service.set_status(claim, status=InspectionStatus.COMPLETED, actor="R. Achebe")

        await service.add_observation(
            claim,
            element="Sprinkler main",
            severity=DamageSeverity.MODERATE,
            finding="Corrosion found on re-inspection.",
            actor="H. Okonjo",
        )
        assert len(claims.observations) == 1


class TestActions:
    @pytest.mark.asyncio
    async def test_an_action_can_be_owned_by_somebody_outside_the_desk(self) -> None:
        """The adjuster, the insured, a contractor — three of the four common owners."""
        service, claims, audit, claim = await commissioned()
        await service.add_action(
            claim,
            label="Provide the stock schedule for the mezzanine",
            owner="Insured — J. Bhatt",
            actor="R. Achebe",
        )

        row = claims.actions[0]
        assert row.owner == "Insured — J. Bhatt"
        assert row.done is False
        assert row.raised_by == "R. Achebe"
        assert audit.types()[-1] == str(AuditEventType.INSPECTION_ACTION_SET)

    @pytest.mark.asyncio
    async def test_ticking_an_action_records_who_and_when(self) -> None:
        service, claims, _, claim = await commissioned()
        await service.add_action(
            claim, label="Chase the stock schedule", owner="R. Achebe", actor="R. Achebe"
        )

        await service.set_action_done(
            claim, action_id=claims.actions[0].id, done=True, actor="D. Mensah"
        )
        assert claims.actions[0].done is True
        assert claims.actions[0].done_by == "D. Mensah"
        assert claims.actions[0].done_at is not None

    @pytest.mark.asyncio
    async def test_unticking_clears_the_attribution_rather_than_keeping_a_stale_one(
        self,
    ) -> None:
        service, claims, _, claim = await commissioned()
        await service.add_action(
            claim, label="Chase the stock schedule", owner="R. Achebe", actor="R. Achebe"
        )
        action_id = claims.actions[0].id

        await service.set_action_done(claim, action_id=action_id, done=True, actor="D. Mensah")
        await service.set_action_done(claim, action_id=action_id, done=False, actor="D. Mensah")

        assert claims.actions[0].done is False
        assert claims.actions[0].done_by is None
        assert claims.actions[0].done_at is None

    @pytest.mark.asyncio
    async def test_ticking_twice_writes_one_event(self) -> None:
        """A retried request is not a second thing having happened."""
        service, claims, audit, claim = await commissioned()
        await service.add_action(
            claim, label="Chase the stock schedule", owner="R. Achebe", actor="R. Achebe"
        )
        action_id = claims.actions[0].id

        await service.set_action_done(claim, action_id=action_id, done=True, actor="D. Mensah")
        await service.set_action_done(claim, action_id=action_id, done=True, actor="D. Mensah")

        assert audit.types().count(str(AuditEventType.INSPECTION_ACTION_SET)) == 2

    @pytest.mark.asyncio
    async def test_an_action_from_another_claim_is_not_found(self) -> None:
        service, _, _, claim = await commissioned()
        with pytest.raises(NotFoundError):
            await service.set_action_done(
                claim, action_id=uuid.uuid4(), done=True, actor="D. Mensah"
            )


# ---------------------------------------------------------------------------
# The line between the layers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_write_pushes_what_it_wrote() -> None:
    """Each of the seven flushes, because each is read back before the response.

    Not a stylistic assertion. The route answers every one of these by rebuilding
    the whole section from queries, so a write that did not flush would return a
    payload describing the claim as it was before the request — the tab would show
    a visit that was not booked and an observation that was not recorded, and only
    a reload would disagree.
    """
    service, claims, _ = build()
    claim = make_claim()

    await service.commission(claim, actor="R. Achebe")
    await service.schedule(claim, scheduled_at=BOOKED, actor="R. Achebe")
    await service.record_attendance(claim, attended_at=ATTENDED, actor="H. Okonjo")
    await service.add_observation(
        claim,
        element="Racking, bays 1-14",
        severity=DamageSeverity.SEVERE,
        finding="Deformed by heat.",
        actor="H. Okonjo",
    )
    action = await service.add_action(
        claim, label="Chase the stock schedule", owner="R. Achebe", actor="R. Achebe"
    )
    await service.set_action_done(claim, action_id=action.id, done=True, actor="R. Achebe")
    await service.set_status(claim, status=InspectionStatus.COMPLETED, actor="R. Achebe")

    assert claims.flushes == 7, f"one of the writes did not flush ({claims.flushes} of 7)"
    # And the reads that follow them see everything, which is the point.
    assert len(claims.observations) == 1
    assert claims.actions[0].done is True
    assert claims.inspection.status == str(InspectionStatus.COMPLETED)


def test_the_domain_layer_reads_no_clock() -> None:
    """The same guarantee `app.domain.coverage` gives, asserted the same way.

    Every timestamp on an inspection is a fact somebody supplied — when the visit
    was booked, when the adjuster arrived. A `now()` inside the rules would make
    the same inputs answer differently tomorrow, and the tests above would be
    describing a moment rather than a machine.
    """
    source = __import__("pathlib").Path(rules.__file__).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "utcnow" not in source


# ---------------------------------------------------------------------------
# Whose visit it is
# ---------------------------------------------------------------------------


class TestWhoMayRecord:
    """Who is allowed to write findings, and whose findings they are.

    The gap this closes: `INSPECTION_WORK_ROLES` named the loss adjuster and
    explained at length why their report is their own work product — and every route
    that *recorded* anything was gated on `CLAIM_WORK_ROLES`, which excludes them.
    The one route they could reach was refused until attendance and an observation
    existed, both of which only a handler could record, so an adjuster could never
    clear a blocker they were not permitted to satisfy.
    """

    @pytest.mark.asyncio
    async def test_a_handler_may_record_on_any_visit(self) -> None:
        """Not laxity. They commission it, they chase it, and on a small loss they
        write down what the adjuster told them on the telephone."""
        service, claims, _, claim = await commissioned()

        inspection = await service.authorise_recording(
            claim,
            roles=["claims-handler"],
            subject="handler-1",
            email="r.achebe@carrier.example",
            actor="R. Achebe",
        )

        assert inspection is claims.inspection
        #: Nothing claimed. A handler recording on somebody else's visit must not
        #: quietly become its adjuster.
        assert claims.inspection.adjuster_subject is None

    @pytest.mark.asyncio
    async def test_an_adjuster_may_record_on_the_visit_instructed_to_them(self) -> None:
        service, claims, _ = build()
        claim = make_claim()
        await service.commission(
            claim,
            actor="R. Achebe",
            adjuster_firm="Crawford & Co",
            adjuster_email="H.Okonjo@Crawford.Example",
        )

        await service.authorise_recording(
            claim,
            roles=["loss-adjuster"],
            subject="kc-9f1",
            email="h.okonjo@crawford.example",
            actor="H. Okonjo",
        )

        #: Adopted: from here the link is by account and survives the address
        #: changing, which is why it is written on the first write rather than never.
        assert claims.inspection.adjuster_subject == "kc-9f1"
        #: The name too, because the row carried none — a firm was instructed and no
        #: person was named.
        assert claims.inspection.adjuster_name == "H. Okonjo"

    @pytest.mark.asyncio
    async def test_an_adjuster_is_refused_on_somebody_elses_visit(self) -> None:
        """The reason the role gate alone was not enough to widen.

        Admitting adjusters without this would let any adjuster record findings onto
        any inspection on the desk, which is worse than the read-only board it
        replaces.
        """
        service, claims, _ = build()
        claim = make_claim()
        await service.commission(
            claim,
            actor="R. Achebe",
            adjuster_email="someone.else@crawford.example",
        )

        with pytest.raises(PermissionDeniedError) as raised:
            await service.authorise_recording(
                claim,
                roles=["loss-adjuster"],
                subject="kc-9f1",
                email="h.okonjo@crawford.example",
                actor="H. Okonjo",
            )

        assert "not assigned to you" in str(raised.value)
        assert claims.inspection.adjuster_subject is None

    @pytest.mark.asyncio
    async def test_an_adjuster_is_refused_on_a_visit_instructed_to_nobody(self) -> None:
        """A firm instructed with no named contact belongs to nobody.

        Not a gap to be filled in by whoever asks first: it is the ordinary state of
        a visit, and it means the handler records the findings — which is what
        happened for every inspection before these columns existed.
        """
        service, _, _, claim = await commissioned()

        with pytest.raises(PermissionDeniedError):
            await service.authorise_recording(
                claim,
                roles=["loss-adjuster"],
                subject="kc-9f1",
                email="h.okonjo@crawford.example",
                actor="H. Okonjo",
            )

    @pytest.mark.asyncio
    async def test_a_claimed_visit_stops_answering_to_the_address(self) -> None:
        """Once an account is on the row it is the answer.

        Otherwise a stale address would re-open the visit to whoever holds that
        mailbox next, which is the failure mode of identifying people by email.
        """
        service, claims, _ = build()
        claim = make_claim()
        await service.commission(
            claim, actor="R. Achebe", adjuster_email="h.okonjo@crawford.example"
        )
        claims.inspection.adjuster_subject = "kc-first"

        with pytest.raises(PermissionDeniedError):
            await service.authorise_recording(
                claim,
                roles=["loss-adjuster"],
                subject="kc-second",
                email="h.okonjo@crawford.example",
                actor="Somebody Else",
            )

    @pytest.mark.asyncio
    async def test_a_handler_who_is_also_an_adjuster_keeps_the_handler_answer(self) -> None:
        """Holding both personas is a union, not a narrowing."""
        service, _, _, claim = await commissioned()

        await service.authorise_recording(
            claim,
            roles=["loss-adjuster", "claims-handler"],
            subject="both-1",
            email="both@carrier.example",
            actor="Both Hats",
        )

    @pytest.mark.asyncio
    async def test_recording_is_refused_before_a_visit_is_commissioned(self) -> None:
        """The 404 comes first: there is nothing to be authorised against."""
        service, _, _ = build()

        with pytest.raises(NotFoundError):
            await service.authorise_recording(
                make_claim(),
                roles=["claims-handler"],
                subject="handler-1",
                email="r.achebe@carrier.example",
                actor="R. Achebe",
            )
