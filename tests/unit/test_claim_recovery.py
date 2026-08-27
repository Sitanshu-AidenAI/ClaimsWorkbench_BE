"""The recovery register: the arithmetic, the machine, and the service that writes both.

Two halves, the shape the other claim suites use.

The first exercises `app.domain.recovery`, which is pure. The cases worth reading
there are the ones where being wrong costs money or flatters a figure: a forecast
added to a bank statement, a written-off row still counted as expected, a total
labelled with a currency it is not in.

The second exercises `ClaimRecoveryService` against an in-memory repository that
defers writes until flushed, because the real session does — the same fake shape
`test_claim_inspection.py` uses, and for the same reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import recovery as rules
from app.domain.enums import AuditEventType, RecoveryKind, RecoveryStatus
from app.services.claims.recoveries import ClaimRecoveryService

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def row(
    status: RecoveryStatus | str = RecoveryStatus.PURSUING,
    *,
    expected: int = 0,
    recovered: int = 0,
    currency: str = "USD",
    label: str = "Subrogation v haulier",
    limitation: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=str(status),
        expected_minor=expected,
        recovered_minor=recovered,
        currency=currency,
        label=label,
        limitation_at=limitation,
    )


# ---------------------------------------------------------------------------
# The money
# ---------------------------------------------------------------------------


class TestTheTwoFigures:
    def test_the_expectation_is_what_is_still_outstanding(self) -> None:
        """The one that stops a reader adding a forecast to a bank statement.

        Reporting the gross 40,000 beside the 30,000 banked would invite the sum
        70,000, which describes nothing.
        """
        rows = [row(expected=40_000_00, recovered=30_000_00)]
        assert rules.expected_minor(rows) == 10_000_00
        assert rules.recovered_minor(rows) == 30_000_00

    def test_a_written_off_row_expects_nothing_and_keeps_what_it_banked(self) -> None:
        """Money that came back before the rest was written off still came back."""
        rows = [row(RecoveryStatus.WRITTEN_OFF, expected=12_000_00, recovered=2_000_00)]
        assert rules.expected_minor(rows) == 0
        assert rules.recovered_minor(rows) == 2_000_00

    def test_over_recovering_is_not_a_negative_expectation(self) -> None:
        rows = [row(RecoveryStatus.RECOVERED, expected=8_000_00, recovered=8_500_00)]
        assert rules.expected_minor(rows) == 0

    def test_an_empty_register_totals_to_nothing(self) -> None:
        assert rules.expected_minor([]) == 0
        assert rules.recovered_minor([]) == 0
        assert rules.single_currency([]) is None

    def test_a_mixed_register_has_no_single_currency(self) -> None:
        """A total labelled with money it is not in is worse than no total."""
        assert rules.single_currency([row(currency="USD"), row(currency="GBP")]) is None
        assert rules.single_currency([row(currency="USD"), row(currency="USD")]) == "USD"

    def test_unassessed_prospects_are_not_nought(self) -> None:
        assert rules.prospects_band(None) == "not assessed"
        assert rules.prospects_band(0.0) == "none"


class TestTransitions:
    def test_an_identified_recovery_is_pursued_or_written_off(self) -> None:
        assert rules.can_transition(RecoveryStatus.IDENTIFIED, RecoveryStatus.PURSUING)
        assert rules.can_transition(RecoveryStatus.IDENTIFIED, RecoveryStatus.WRITTEN_OFF)
        #: Not banked straight from identified: money cannot arrive on a recovery
        #: nobody has pursued.
        assert not rules.can_transition(RecoveryStatus.IDENTIFIED, RecoveryStatus.RECOVERED)

    def test_a_collapsed_negotiation_goes_back_to_being_chased(self) -> None:
        assert rules.can_transition(RecoveryStatus.IN_NEGOTIATION, RecoveryStatus.PURSUING)

    def test_a_written_off_recovery_can_be_revived(self) -> None:
        """A third party's insurer suddenly engaging is real, and welcome.

        Forcing a second recovery for the same money would double-count the
        expectation.
        """
        assert rules.can_transition(RecoveryStatus.WRITTEN_OFF, RecoveryStatus.PURSUING)

    def test_a_recovered_recovery_is_terminal(self) -> None:
        assert rules.allowed_transitions(RecoveryStatus.RECOVERED) == frozenset()

    def test_an_unknown_status_is_refused_rather_than_raising(self) -> None:
        assert rules.can_transition("chasing", RecoveryStatus.RECOVERED) is False


class TestLimitation:
    def test_a_recovery_past_its_limitation_says_how_long_ago(self) -> None:
        warnings = rules.limitation_warnings([row(limitation=NOW - timedelta(days=9))], now=NOW)
        assert warnings == ["Subrogation v haulier passed its limitation date 9 days ago."]

    def test_one_coming_up_says_how_long_is_left(self) -> None:
        warnings = rules.limitation_warnings([row(limitation=NOW + timedelta(days=1))], now=NOW)
        assert warnings == ["Subrogation v haulier is barred in 1 day."]

    def test_a_distant_limitation_is_not_a_warning(self) -> None:
        """An inspection due in eight months is not news."""
        assert rules.limitation_warnings([row(limitation=NOW + timedelta(days=240))], now=NOW) == []

    def test_a_closed_recovery_cannot_be_barred(self) -> None:
        """Nothing is left to lose on a recovered or written-off file."""
        closed = [
            row(RecoveryStatus.RECOVERED, limitation=NOW - timedelta(days=30)),
            row(RecoveryStatus.WRITTEN_OFF, limitation=NOW - timedelta(days=30)),
        ]
        assert rules.limitation_warnings(closed, now=NOW) == []


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class FakeClaimRepo:
    """Defers writes until flushed, because `autoflush=False` on the real session.

    The same fake shape `test_claim_inspection.py` documents, and for the same
    reason: a double that made every write immediately readable would pass a service
    that never flushed and then fail in production.
    """

    def __init__(self) -> None:
        self.recoveries: list[Any] = []
        self.events: list[Any] = []
        self.tasks: list[Any] = []
        self._pending: list[tuple[str, Any]] = []
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1
        for kind, item in self._pending:
            getattr(self, kind).append(item)
        self._pending.clear()

    async def list_recoveries(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self.recoveries)

    async def get_recovery(self, claim_id: uuid.UUID, recovery_id: uuid.UUID) -> Any | None:
        del claim_id
        return next((item for item in self.recoveries if item.id == recovery_id), None)

    def add_recovery(self, item: Any) -> Any:
        item.id = uuid.uuid4()
        self._pending.append(("recoveries", item))
        return item

    def add_recovery_event(self, item: Any) -> Any:
        item.id = uuid.uuid4()
        self._pending.append(("events", item))
        return item

    def add_recovery_task(self, item: Any) -> Any:
        item.id = uuid.uuid4()
        self._pending.append(("tasks", item))
        return item

    async def get_recovery_task(self, claim_id: uuid.UUID, task_id: uuid.UUID) -> Any | None:
        del claim_id
        return next((item for item in self.tasks if item.id == task_id), None)


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
        "reference": "CLM-2026-000009",
        "status": "in_review",
        "currency": "USD",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build() -> tuple[ClaimRecoveryService, FakeClaimRepo, RecordingAudit]:
    claims = FakeClaimRepo()
    audit = RecordingAudit()
    return (
        ClaimRecoveryService(claims, audit),  # type: ignore[arg-type]
        claims,
        audit,
    )


class TestOpening:
    @pytest.mark.asyncio
    async def test_a_recovery_opens_identified_with_its_own_first_event(self) -> None:
        service, repo, audit = build()
        claim = make_claim()

        recovery = await service.open(
            claim,
            kind=RecoveryKind.SUBROGATION,
            label="Subrogation against the haulier",
            actor="R. Achebe",
            expected_minor=40_000_00,
        )

        assert recovery.status == str(RecoveryStatus.IDENTIFIED)
        assert recovery.recovered_minor == 0
        assert len(repo.events) == 1
        assert audit.types() == [str(AuditEventType.RECOVERY_OPENED)]

    @pytest.mark.asyncio
    async def test_many_recoveries_per_claim(self) -> None:
        """Unlike the inspection. One fire produces three routes back at once."""
        service, repo, _ = build()
        claim = make_claim()

        for kind in (RecoveryKind.SUBROGATION, RecoveryKind.SALVAGE, RecoveryKind.REINSURANCE):
            await service.open(claim, kind=kind, label=str(kind), actor="R. Achebe")

        assert len(repo.recoveries) == 3

    @pytest.mark.asyncio
    async def test_unassessed_prospects_stay_absent(self) -> None:
        service, repo, _ = build()
        await service.open(
            make_claim(), kind=RecoveryKind.SALVAGE, label="Salvage", actor="R. Achebe"
        )
        assert repo.recoveries[0].prospects is None

    @pytest.mark.asyncio
    async def test_a_currency_the_claim_is_not_booked_in_is_refused(self) -> None:
        """A register in two currencies has no total anybody can state."""
        service, _, _ = build()
        with pytest.raises(ValidationError) as raised:
            await service.open(
                make_claim(currency="USD"),
                kind=RecoveryKind.SUBROGATION,
                label="Subrogation",
                actor="R. Achebe",
                expected_minor=1_000_00,
                currency="GBP",
            )
        assert "booked in USD" in str(raised.value)

    @pytest.mark.asyncio
    async def test_prospects_outside_a_probability_are_refused(self) -> None:
        service, _, _ = build()
        with pytest.raises(ValidationError):
            await service.open(
                make_claim(),
                kind=RecoveryKind.SALVAGE,
                label="Salvage",
                actor="R. Achebe",
                prospects=1.4,
            )


class TestProgress:
    async def _opened(self, **claim_overrides: Any) -> tuple[Any, ...]:
        service, repo, audit = build()
        claim = make_claim(**claim_overrides)
        recovery = await service.open(
            claim,
            kind=RecoveryKind.SUBROGATION,
            label="Subrogation against the haulier",
            actor="R. Achebe",
            expected_minor=40_000_00,
        )
        return service, repo, audit, claim, recovery

    @pytest.mark.asyncio
    async def test_banking_money_records_what_arrived_not_the_total(self) -> None:
        """The audit line names the payment; the column holds the running total."""
        service, repo, audit, claim, recovery = await self._opened()

        await service.progress(
            claim, recovery_id=recovery.id, actor="R. Achebe", status=RecoveryStatus.PURSUING
        )
        await service.progress(
            claim, recovery_id=recovery.id, actor="R. Achebe", recovered_minor=30_000_00
        )

        assert repo.recoveries[0].recovered_minor == 30_000_00
        assert "banked 30,000.00 USD" in audit.summaries()[-1]
        #: Money arriving is the material event a handler skims the log for.
        assert audit.types()[-1] == str(AuditEventType.RECOVERY_RECORDED)

    @pytest.mark.asyncio
    async def test_a_second_payment_is_the_new_total(self) -> None:
        service, repo, audit, claim, recovery = await self._opened()
        await service.progress(
            claim, recovery_id=recovery.id, actor="R. Achebe", recovered_minor=10_000_00
        )
        await service.progress(
            claim, recovery_id=recovery.id, actor="R. Achebe", recovered_minor=25_000_00
        )

        assert repo.recoveries[0].recovered_minor == 25_000_00
        assert "banked 15,000.00 USD" in audit.summaries()[-1]

    @pytest.mark.asyncio
    async def test_a_lower_recovered_figure_is_refused(self) -> None:
        """A correction to a money column belongs in a ledger, not an overwrite."""
        service, _, _, claim, recovery = await self._opened()
        await service.progress(
            claim, recovery_id=recovery.id, actor="R. Achebe", recovered_minor=30_000_00
        )

        with pytest.raises(ConflictError) as raised:
            await service.progress(
                claim, recovery_id=recovery.id, actor="R. Achebe", recovered_minor=20_000_00
            )
        assert "running total" in str(raised.value)

    @pytest.mark.asyncio
    async def test_an_illegal_move_names_it(self) -> None:
        service, _, _, claim, recovery = await self._opened()
        with pytest.raises(ConflictError) as raised:
            await service.progress(
                claim, recovery_id=recovery.id, actor="R. Achebe", status=RecoveryStatus.RECOVERED
            )
        assert "identified" in str(raised.value)

    @pytest.mark.asyncio
    async def test_progress_with_nothing_to_say_is_refused(self) -> None:
        """An event with no content is a line in the history that explains nothing."""
        service, _, _, claim, recovery = await self._opened()
        with pytest.raises(ValidationError):
            await service.progress(claim, recovery_id=recovery.id, actor="R. Achebe")

    @pytest.mark.asyncio
    async def test_a_note_alone_is_enough(self) -> None:
        service, repo, _, claim, recovery = await self._opened()
        await service.progress(
            claim, recovery_id=recovery.id, actor="R. Achebe", note="Their carrier acknowledged."
        )
        assert repo.events[-1].description == "Their carrier acknowledged."

    @pytest.mark.asyncio
    async def test_a_recovery_from_another_claim_is_not_found(self) -> None:
        service, _, _, claim, _ = await self._opened()
        with pytest.raises(NotFoundError):
            await service.progress(claim, recovery_id=uuid.uuid4(), actor="R. Achebe")


class TestTasks:
    @pytest.mark.asyncio
    async def test_a_task_need_not_belong_to_a_recovery(self) -> None:
        """ "Obtain the police report" is recovery work before anybody knows which."""
        (
            service,
            repo,
            audit,
        ) = build()
        claim = make_claim()

        await service.add_task(
            claim, label="Obtain the police report", owner="Insured", actor="R. Achebe"
        )

        assert repo.tasks[0].recovery_id is None
        assert audit.types()[-1] == str(AuditEventType.RECOVERY_TASK_SET)

    @pytest.mark.asyncio
    async def test_a_task_against_a_recovery_that_does_not_exist_is_refused(self) -> None:
        service, _, _ = build()
        with pytest.raises(NotFoundError):
            await service.add_task(
                make_claim(),
                label="Chase the solicitor",
                owner="R. Achebe",
                actor="R. Achebe",
                recovery_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_ticking_records_who_and_when_and_unticking_clears_it(self) -> None:
        service, repo, _ = build()
        claim = make_claim()
        await service.add_task(claim, label="Chase", owner="R. Achebe", actor="R. Achebe")
        task_id = repo.tasks[0].id

        await service.set_task_done(claim, task_id=task_id, done=True, actor="D. Mensah")
        assert repo.tasks[0].done_by == "D. Mensah"
        assert repo.tasks[0].done_at is not None

        await service.set_task_done(claim, task_id=task_id, done=False, actor="D. Mensah")
        assert repo.tasks[0].done_by is None
        assert repo.tasks[0].done_at is None


@pytest.mark.asyncio
async def test_every_write_pushes_what_it_wrote() -> None:
    """Each is read back before the response, so none may skip the flush.

    Sessions are built with `autoflush=False` — the defect this guards is the one
    the inspection service shipped with, where commissioning and booking in one
    request answered "no inspection has been commissioned".
    """
    service, repo, _ = build()
    claim = make_claim()

    recovery = await service.open(
        claim, kind=RecoveryKind.SALVAGE, label="Salvage", actor="R. Achebe"
    )
    await service.progress(claim, recovery_id=recovery.id, actor="R. Achebe", note="Listed.")
    await service.add_task(claim, label="Chase", owner="R. Achebe", actor="R. Achebe")
    await service.set_task_done(claim, task_id=repo.tasks[0].id, done=True, actor="R. Achebe")

    #: Opening flushes twice — the row, then its first event, which needs the id.
    assert repo.flushes == 5
    assert len(repo.recoveries) == 1
    assert len(repo.events) == 2
    assert repo.tasks[0].done is True


def test_the_domain_layer_reads_no_clock() -> None:
    """`limitation_warnings` takes `now`, for the reason the other machines do."""
    source = __import__("pathlib").Path(rules.__file__).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "utcnow" not in source
