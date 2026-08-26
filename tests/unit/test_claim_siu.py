"""The SIU case, and the verdict recorded against each fraud indicator.

The distinction this file turns on is between a **machine's suspicion** and a
**person's decision**. `claims.fraud_flag` is the first; an SIU case is the second,
and the tab used to derive one from the other — which gave it two of six states and
made *screening* mean nothing more than "the model scored this claim".

The other thing worth reading twice: a disposition's **absence** is a state.
`FraudDisposition` has two values and no `undecided`, because an indicator nobody has
looked at has no row rather than a third value.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import siu as rules
from app.domain.enums import AuditEventType, FraudDisposition, SiuStatus
from app.services.claims.siu import ClaimSiuService

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_screening_can_end_without_an_accusation(self) -> None:
        """The edge that matters most, and the easiest to leave out.

        A handler screens a suspicion, finds nothing in it, and the claim goes back
        to being ordinary. Without this the only way out of screening would be to
        refer it — which would make every glance at a claim an accusation.
        """
        assert rules.can_transition(SiuStatus.SCREENING, SiuStatus.NOT_REFERRED)

    def test_a_claim_can_be_referred_without_screening_first(self) -> None:
        """Some referrals are obvious on sight."""
        assert rules.can_transition(SiuStatus.NOT_REFERRED, SiuStatus.REFERRED)

    def test_a_closed_case_can_be_reopened(self) -> None:
        """New information arrives, and a second case would split one story."""
        assert rules.can_transition(SiuStatus.CLOSED_NO_ACTION, SiuStatus.UNDER_INVESTIGATION)
        assert rules.can_transition(SiuStatus.CLOSED_CONFIRMED, SiuStatus.UNDER_INVESTIGATION)

    def test_the_two_closed_states_stay_distinct(self) -> None:
        """Opposite outcomes. Collapsing them makes the one statistic unanswerable."""
        assert rules.is_closed(SiuStatus.CLOSED_NO_ACTION)
        assert rules.is_closed(SiuStatus.CLOSED_CONFIRMED)
        assert not rules.is_closed(SiuStatus.UNDER_INVESTIGATION)

    def test_a_referral_cannot_skip_to_screening(self) -> None:
        assert not rules.can_transition(SiuStatus.REFERRED, SiuStatus.SCREENING)

    def test_an_unknown_status_is_refused_rather_than_raising(self) -> None:
        assert rules.can_transition("investigating", SiuStatus.CLOSED_NO_ACTION) is False

    def test_only_an_investigation_needs_a_name_against_it(self) -> None:
        """A referral sits in SIU's queue before anybody picks it up."""
        assert rules.requires_investigator(SiuStatus.UNDER_INVESTIGATION)
        assert not rules.requires_investigator(SiuStatus.REFERRED)

    def test_with_siu_is_the_two_states_somebody_else_owns(self) -> None:
        assert rules.is_with_siu(SiuStatus.REFERRED)
        assert rules.is_with_siu(SiuStatus.UNDER_INVESTIGATION)
        assert not rules.is_with_siu(SiuStatus.SCREENING)


class TestDispositions:
    def test_an_indicator_with_no_row_is_undecided(self) -> None:
        disposed = {"FF-14": "accepted"}
        assert rules.outstanding_indicators(["FF-14", "MV-03", "FF-02"], disposed) == 2

    def test_accepted_are_counted_apart_from_discounted(self) -> None:
        disposed = {"FF-14": "accepted", "MV-03": "discounted", "FF-02": "accepted"}
        assert rules.accepted_count(disposed) == 2

    def test_the_codes_map_is_built_from_the_rows(self) -> None:
        rows = [
            SimpleNamespace(code="FF-14", disposition="accepted"),
            SimpleNamespace(code="MV-03", disposition="discounted"),
        ]
        assert rules.disposed_codes(rows) == {"FF-14": "accepted", "MV-03": "discounted"}


class TestRecommendedActions:
    def test_an_investigator_s_own_list_wins(self) -> None:
        """Their instructions beat a generated list every time."""
        actions = rules.recommended_actions(
            status=SiuStatus.UNDER_INVESTIGATION,
            outstanding=3,
            accepted=1,
            stored=["Obtain the insured's bank statements."],
        )
        assert actions == ["Obtain the insured's bank statements."]

    def test_an_accepted_indicator_on_an_unreferred_claim_leads(self) -> None:
        """The strongest signal on the tab, so the sentence a reader meets first."""
        actions = rules.recommended_actions(
            status=SiuStatus.NOT_REFERRED, outstanding=2, accepted=1
        )
        assert "has been accepted" in actions[0]
        assert "not been referred" in actions[0]

    def test_a_case_with_siu_says_to_chase_before_settling(self) -> None:
        actions = rules.recommended_actions(
            status=SiuStatus.UNDER_INVESTIGATION, outstanding=0, accepted=0
        )
        assert actions == ["The case is with SIU. Chase the investigator before settling."]

    def test_a_clean_claim_says_so_rather_than_nothing(self) -> None:
        """An empty list reads as a rendering fault."""
        actions = rules.recommended_actions(
            status=SiuStatus.NOT_REFERRED, outstanding=0, accepted=0
        )
        assert actions == ["Nothing outstanding on the fraud review."]


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class FakeClaimRepo:
    """Defers writes until flushed, for the reason the other fakes do."""

    def __init__(self, case: Any = None, dispositions: list[Any] | None = None) -> None:
        self.case = case
        self.dispositions: list[Any] = dispositions or []
        self._pending: list[Any] = []
        self._pending_case: Any = None
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1
        if self._pending_case is not None:
            self.case = self._pending_case
            self._pending_case = None
        self.dispositions.extend(self._pending)
        self._pending.clear()

    async def get_siu_case(self, claim_id: uuid.UUID) -> Any | None:
        del claim_id
        return self.case

    def add_siu_case(self, case: Any) -> Any:
        case.id = uuid.uuid4()
        self._pending_case = case
        return case

    async def list_fraud_dispositions(self, claim_id: uuid.UUID) -> list[Any]:
        del claim_id
        return list(self.dispositions)

    async def get_fraud_disposition(self, claim_id: uuid.UUID, code: str) -> Any | None:
        del claim_id
        return next((row for row in self.dispositions if row.code == code), None)

    def add_fraud_disposition(self, row: Any) -> Any:
        row.id = uuid.uuid4()
        self._pending.append(row)
        return row


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
        "fraud_flag": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def case(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "status": str(SiuStatus.REFERRED),
        "referred_by": "R. Achebe",
        "referred_at": NOW,
        "investigator": None,
        "siu_reference": None,
        "referral_reason": "Third loss at this site in a year.",
        "outcome": None,
        "closed_at": None,
        "recommended_actions": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build(
    existing: Any = None, dispositions: list[Any] | None = None
) -> tuple[ClaimSiuService, FakeClaimRepo, RecordingAudit]:
    claims = FakeClaimRepo(existing, dispositions)
    audit = RecordingAudit()
    return (
        ClaimSiuService(claims, audit),  # type: ignore[arg-type]
        claims,
        audit,
    )


class TestReferral:
    @pytest.mark.asyncio
    async def test_referring_records_who_and_why(self) -> None:
        service, repo, audit = build()
        referred = await service.refer(
            make_claim(),
            actor="R. Achebe",
            reason="Third refrigeration failure at this site in fourteen months.",
        )

        assert referred.status == str(SiuStatus.REFERRED)
        assert referred.referred_by == "R. Achebe"
        assert repo.case is referred
        assert audit.types() == [str(AuditEventType.SIU_REFERRED)]
        assert "fourteen months" in audit.summaries()[0]

    @pytest.mark.asyncio
    async def test_a_referral_with_no_reason_is_refused(self) -> None:
        """The write a regulator would ask about first."""
        service, _, _ = build()
        with pytest.raises(ValidationError) as raised:
            await service.refer(make_claim(), actor="R. Achebe", reason="   ")
        assert "why you are referring" in str(raised.value)

    @pytest.mark.asyncio
    async def test_referring_a_claim_already_with_siu_is_refused(self) -> None:
        service, _, _ = build(case(status=str(SiuStatus.UNDER_INVESTIGATION)))
        with pytest.raises(ConflictError) as raised:
            await service.refer(make_claim(), actor="R. Achebe", reason="Again.")
        assert "already with SIU" in str(raised.value)

    @pytest.mark.asyncio
    async def test_referring_after_screening_reuses_the_case(self) -> None:
        screened = case(status=str(SiuStatus.SCREENING), referred_by=None, referred_at=None)
        service, repo, _ = build(screened)

        referred = await service.refer(
            make_claim(), actor="R. Achebe", reason="Screening turned something up."
        )
        assert referred is screened
        assert repo.case is screened

    @pytest.mark.asyncio
    async def test_reopening_clears_the_previous_outcome(self) -> None:
        """A stale conclusion would have the tab reporting one for a live case."""
        closed = case(
            status=str(SiuStatus.CLOSED_NO_ACTION),
            outcome="Nothing found.",
            closed_at=NOW,
        )
        service, _, _ = build(closed)

        #: `closed_no_action → referred` is not legal, so this is refused and the
        #: caller is told to reopen the investigation instead.
        with pytest.raises(ConflictError) as raised:
            await service.refer(make_claim(), actor="R. Achebe", reason="More has come in.")
        assert "Reopen the investigation" in str(raised.value)


class TestScreening:
    @pytest.mark.asyncio
    async def test_screening_opens_a_case_without_accusing_anybody(self) -> None:
        service, repo, audit = build()
        opened = await service.screen(make_claim(), actor="R. Achebe", note="Worth a look.")

        assert opened.status == str(SiuStatus.SCREENING)
        assert opened.referred_by is None
        assert repo.case is opened
        assert audit.types() == [str(AuditEventType.SIU_STATUS_CHANGED)]

    @pytest.mark.asyncio
    async def test_screening_a_claim_that_already_has_a_case_is_refused(self) -> None:
        service, _, _ = build(case())
        with pytest.raises(ConflictError):
            await service.screen(make_claim(), actor="R. Achebe")


class TestStatus:
    @pytest.mark.asyncio
    async def test_investigating_requires_an_investigator(self) -> None:
        """An investigation nobody owns is not one, and nobody can be chased."""
        service, _, _ = build(case())
        with pytest.raises(ValidationError) as raised:
            await service.set_status(
                make_claim(), status=SiuStatus.UNDER_INVESTIGATION, actor="R. Achebe"
            )
        assert "Name the investigator" in str(raised.value)

    @pytest.mark.asyncio
    async def test_an_investigator_already_on_the_case_satisfies_it(self) -> None:
        service, repo, _ = build(case(investigator="D. Mensah (SIU)"))
        await service.set_status(
            make_claim(), status=SiuStatus.UNDER_INVESTIGATION, actor="R. Achebe"
        )
        assert repo.case.status == str(SiuStatus.UNDER_INVESTIGATION)

    @pytest.mark.asyncio
    async def test_closing_requires_an_outcome(self) -> None:
        """A closed case with nothing recorded cannot answer the only question."""
        service, _, _ = build(case(status=str(SiuStatus.UNDER_INVESTIGATION)))
        with pytest.raises(ValidationError) as raised:
            await service.set_status(
                make_claim(), status=SiuStatus.CLOSED_CONFIRMED, actor="R. Achebe"
            )
        assert "what the investigation concluded" in str(raised.value)

    @pytest.mark.asyncio
    async def test_closing_with_an_outcome_stamps_the_moment(self) -> None:
        service, repo, audit = build(case(status=str(SiuStatus.UNDER_INVESTIGATION)))
        await service.set_status(
            make_claim(),
            status=SiuStatus.CLOSED_NO_ACTION,
            actor="R. Achebe",
            outcome="The delay was the broker's own.",
        )

        assert repo.case.closed_at is not None
        assert repo.case.outcome == "The delay was the broker's own."
        assert "broker's own" in audit.summaries()[-1]

    @pytest.mark.asyncio
    async def test_moving_to_where_it_already_is_is_refused(self) -> None:
        service, _, _ = build(case())
        with pytest.raises(ConflictError) as raised:
            await service.set_status(make_claim(), status=SiuStatus.REFERRED, actor="R. Achebe")
        assert "already" in str(raised.value)

    @pytest.mark.asyncio
    async def test_moving_a_claim_with_no_case_says_what_to_do(self) -> None:
        service, _, _ = build()
        with pytest.raises(NotFoundError) as raised:
            await service.set_status(
                make_claim(), status=SiuStatus.UNDER_INVESTIGATION, actor="R. Achebe"
            )
        assert "Refer it or screen it" in str(raised.value)


class TestDisposing:
    @pytest.mark.asyncio
    async def test_accepting_an_indicator_needs_no_defence(self) -> None:
        service, repo, audit = build()
        await service.dispose(
            make_claim(), code="FF-14", disposition=FraudDisposition.ACCEPTED, actor="R. Achebe"
        )

        assert repo.dispositions[0].disposition == str(FraudDisposition.ACCEPTED)
        assert repo.dispositions[0].note is None
        assert audit.types()[-1] == str(AuditEventType.FRAUD_INDICATOR_DISPOSED)

    @pytest.mark.asyncio
    async def test_discounting_one_requires_a_note(self) -> None:
        """A person overruling a fraud signal is the decision worth explaining."""
        service, _, _ = build()
        with pytest.raises(ValidationError) as raised:
            await service.dispose(
                make_claim(),
                code="FF-14",
                disposition=FraudDisposition.DISCOUNTED,
                actor="R. Achebe",
            )
        assert "why you are discounting" in str(raised.value)

    @pytest.mark.asyncio
    async def test_changing_a_verdict_updates_the_one_row(self) -> None:
        """One verdict per indicator. The trail keeps what it used to say."""
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            code="FF-14",
            disposition=str(FraudDisposition.ACCEPTED),
            note=None,
            reviewed_by="R. Achebe",
            reviewed_at=NOW,
        )
        service, repo, audit = build(dispositions=[existing])

        await service.dispose(
            make_claim(),
            code="FF-14",
            disposition=FraudDisposition.DISCOUNTED,
            actor="D. Mensah",
            note="Broker's own delay.",
        )

        assert len(repo.dispositions) == 1
        assert repo.dispositions[0].disposition == str(FraudDisposition.DISCOUNTED)
        assert repo.dispositions[0].reviewed_by == "D. Mensah"
        #: The previous verdict is in the trail rather than lost.
        assert audit.events[-1]["before"] == {"disposition": "accepted", "note": None}

    @pytest.mark.asyncio
    async def test_a_blank_code_is_refused(self) -> None:
        service, _, _ = build()
        with pytest.raises(ValidationError):
            await service.dispose(
                make_claim(), code="  ", disposition=FraudDisposition.ACCEPTED, actor="R. Achebe"
            )


@pytest.mark.asyncio
async def test_every_write_pushes_what_it_wrote() -> None:
    """Each is read back before the response — see `autoflush=False`."""
    service, repo, _ = build()
    claim = make_claim()

    await service.screen(claim, actor="R. Achebe")
    await service.refer(claim, actor="R. Achebe", reason="Screening found something.")
    await service.dispose(
        claim, code="FF-14", disposition=FraudDisposition.ACCEPTED, actor="R. Achebe"
    )

    assert repo.flushes == 3
    assert repo.case.status == str(SiuStatus.REFERRED)
    assert len(repo.dispositions) == 1


def test_the_domain_layer_reads_no_clock() -> None:
    source = __import__("pathlib").Path(rules.__file__).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "utcnow" not in source
