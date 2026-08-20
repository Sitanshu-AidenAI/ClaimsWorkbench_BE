"""The FNOL journey, end to end, against a real database.

Marked `integration` because it needs Postgres. It is the test that proves the
pieces fit: ingest a broker email, run the pipeline, correct a field, confirm the
policy, create the claim, and find it on the claims queue — with the audit trail
following the whole way.

The model provider is deliberately absent, so the deterministic reader runs. That
is what makes this test cheap enough to run on every commit, and it is also the
path a claims desk falls back to when the provider is down.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.api.deps.services import build_context
from app.core.config import settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import (
    AnalysisKind,
    ClaimStatus,
    DuplicateResolution,
    ExceptionCode,
    FNOLStatus,
)
from app.models.audit import AuditEvent
from app.models.claim import Claim, ClaimAssignment, ClaimTriage
from app.models.fnol import (
    FNOLAIAnalysis,
    FNOLCase,
    FNOLDocument,
    FNOLDuplicateCandidate,
    FNOLException,
    FNOLExtractedField,
    FNOLNote,
    FNOLParty,
    FNOLPolicyMatch,
)
from app.models.reference_data import CatEvent, Handler, Policy
from app.services.ai.base import AIProviderError
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.fnol.claims import ClaimCreationBlocked
from app.services.fnol.ingestion import IncomingAttachment, IncomingEmail
from app.services.fnol.matching import DuplicateDetectionService

pytestmark = pytest.mark.integration

ACTOR = "Integration Test"


def broker_email(*, message_id: str, days_ago: int = 3) -> IncomingEmail:
    loss = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%d/%m/%Y")
    return IncomingEmail(
        sender="claims@testbrokers.co.uk",
        recipient="fnol@carrier.com",
        subject="FNOL - Testfield Manufacturing - fire - TST-2026-0001",
        message_id=message_id,
        from_broker=True,
        body=f"""Policy number: TST-2026-0001
Insured: Testfield Manufacturing Limited
Reported by: Alice Wren
Contact email: a.wren@testbrokers.co.uk

Date of loss: {loss}
Location: Bay 4, Cropwell Industrial Estate, Nottingham NG12 3AB
Country: United Kingdom
Cause: fire
Description: A fire started in the finishing shop overnight and spread to the
adjoining store. The sprinklers operated and the fire brigade attended.
Affected property: finishing shop, store, stock
Injuries: none
Estimated loss: GBP 320,000
""",
        attachments=[
            IncomingAttachment(
                "estimate.csv",
                b"Item,Cost\nMake safe,12000.00\nReinstate finishing shop,180000.00\n",
                "text/csv",
            )
        ],
    )


@pytest.fixture(autouse=True)
def _documents(tmp_path: Path) -> AsyncIterator[None]:
    """Attachment bytes go to a temporary directory, not to the configured store.

    Every other integration module already does this; this one did not, and it was
    the only one that uploaded through whatever `CWB_FNOL_DOCUMENT_STORE` happened
    to name. The default is `s3`, so on any machine without MinIO — CI, or a laptop
    with no `.env` — five tests failed inside `put_object` against an endpoint
    nothing serves. What they exist to prove is the FNOL journey, and that does not
    depend on where the bytes land.
    """
    set_document_store(FilesystemDocumentStore(tmp_path))
    yield
    set_document_store(None)


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    """The engine, per test.

    Function-scoped rather than module-scoped because the event loop is: an async
    fixture outliving the loop its connections were opened on is the classic way
    to get an unexplained hang halfway through a suite.
    """
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture
async def session() -> AsyncIterator[object]:
    """A session whose writes are removed again afterwards.

    The FNOL pipeline commits per stage in places, so a transaction-rollback
    fixture would not hold. Instead the test's own rows are deleted by reference
    prefix, which leaves a shared development database usable.
    """
    factory = get_session_factory()
    async with factory() as db:
        await _install_reference_data(db)
        await db.commit()
        yield db
        await _cleanup(db)
        await db.commit()


async def _install_reference_data(db: object) -> None:
    existing = (
        (await db.execute(select(Policy).where(Policy.policy_number == "TST-2026-0001")))
        .scalars()
        .first()
    )
    if existing is None:
        db.add(
            Policy(
                policy_number="TST-2026-0001",
                insured_name="Testfield Manufacturing Limited",
                insured_organisation="Testfield Manufacturing Limited",
                insured_email="ops@testfield.example",
                broker_name="Test Brokers",
                policy_type="Commercial Combined",
                line_of_business="property",
                status="active",
                effective_date=(datetime.now(UTC) - timedelta(days=300)).date(),
                expiry_date=(datetime.now(UTC) + timedelta(days=60)).date(),
                country="United Kingdom",
                region="Nottinghamshire",
                primary_location="Cropwell Industrial Estate, Nottingham NG12 3AB",
                currency="GBP",
                limit_amount_minor=4_000_000_00,
                deductible_amount_minor=10_000_00,
                perils_covered=["fire", "flood", "storm"],
                exclusions=["terrorism"],
            )
        )

    handler = (
        (await db.execute(select(Handler).where(Handler.email == "int.tester@carrier.com")))
        .scalars()
        .first()
    )
    if handler is None:
        db.add(
            Handler(
                full_name="Integration Handler",
                email="int.tester@carrier.com",
                team="Complex",
                skills=["property", "major_loss", "complex"],
                lines_of_business=["property"],
                countries=["United Kingdom"],
                max_severity="critical",
                authority_limit_minor=1_000_000_00,
                open_claims=0,
                capacity=20,
            )
        )


async def _cleanup(db: object) -> None:
    """Remove this test's rows, children before parents.

    Claims go before notices: `claims.fnol_case_id` is `RESTRICT`, so a notice
    cannot be deleted while the claim it became still points at it.
    """
    case_ids = list(
        (
            await db.execute(
                select(FNOLCase.id).where(FNOLCase.source_body.ilike("%Testfield Manufacturing%"))
            )
        )
        .scalars()
        .all()
    )
    if not case_ids:
        await db.execute(delete(Policy).where(Policy.policy_number == "TST-2026-0001"))
        await db.execute(delete(Handler).where(Handler.email == "int.tester@carrier.com"))
        return

    claim_ids = list(
        (await db.execute(select(Claim.id).where(Claim.fnol_case_id.in_(case_ids)))).scalars().all()
    )

    if claim_ids:
        await db.execute(delete(ClaimAssignment).where(ClaimAssignment.claim_id.in_(claim_ids)))
        await db.execute(delete(ClaimTriage).where(ClaimTriage.claim_id.in_(claim_ids)))
        await db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(claim_ids)))
        await db.execute(delete(Claim).where(Claim.id.in_(claim_ids)))

    for model in (
        FNOLAIAnalysis,
        FNOLDuplicateCandidate,
        FNOLException,
        FNOLExtractedField,
        FNOLNote,
        FNOLParty,
        FNOLPolicyMatch,
        FNOLDocument,
    ):
        await db.execute(delete(model).where(model.fnol_case_id.in_(case_ids)))
    await db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(case_ids)))
    await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))

    await db.execute(delete(Policy).where(Policy.policy_number == "TST-2026-0001"))
    await db.execute(delete(Handler).where(Handler.email == "int.tester@carrier.com"))


async def ingest_and_process(context: object, *, message_id: str) -> FNOLCase:
    email = broker_email(message_id=message_id)
    notification = context.ingestion.from_email(email)
    case, created = await context.ingestion.ingest(notification, actor=ACTOR)
    if created:
        for attachment in email.attachments:
            await context.fnol.attach_document(
                case,
                filename=attachment.filename,
                content=attachment.content,
                content_type=attachment.content_type,
                source="email_attachment",
                actor=ACTOR,
            )
        await context.pipeline.run(case)
    return case


class TestIntakeToClaim:
    async def test_the_whole_journey(self, session: object) -> None:
        context = build_context(session, None)

        # --- Ingest ---------------------------------------------------------
        case = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        assert case.reference.startswith("FNOL-")
        assert case.channel == "broker_email"
        assert case.source_body and "Testfield Manufacturing" in case.source_body

        # --- Read -----------------------------------------------------------
        assert case.policy_number == "TST-2026-0001"
        assert case.insured_name == "Testfield Manufacturing Limited"
        assert case.estimated_loss_minor == 320_000_00
        assert case.line_of_business == "property"
        assert case.date_of_loss is not None

        # The attachment, plus the notification body written out as a document of
        # its own — which is what makes a value read from the broker's email
        # citable in the same way as one read from a file they attached.
        documents = {
            document.filename: document for document in await context.cases.list_documents(case.id)
        }
        assert set(documents) == {"estimate.csv", "notification-body.txt"}
        assert documents["estimate.csv"].extraction_status == "extracted"
        assert documents["notification-body.txt"].source == "email_body"
        assert documents["notification-body.txt"].extracted_text == (case.source_body or "").strip()

        # --- Intelligence ---------------------------------------------------
        analyses = {analysis.kind for analysis in await context.cases.list_analyses(case.id)}
        assert analyses >= {
            "extraction",
            "classification",
            "policy_identification",
            "completeness",
            "severity",
            "fraud",
            "coverage",
            "duplicates",
            "catastrophe",
            "summary",
        }
        assert case.completeness_score is not None and case.completeness_score > 0.5
        assert case.severity in {"high", "medium", "critical"}
        assert case.ai_summary and "Testfield" in case.ai_summary

        candidates = await context.cases.list_policy_matches(case.id)
        assert [candidate.match_strength for candidate in candidates] == ["exact"]
        assert case.policy_id is not None

        # --- Human correction ------------------------------------------------
        changed = await context.fnol.update_fields(
            case,
            {"financial.estimated_loss": "GBP 345,000"},
            actor=ACTOR,
            reason="Revised after the adjuster's visit",
        )
        assert changed == ["financial.estimated_loss"]
        assert case.estimated_loss_minor == 345_000_00

        field = await context.cases.get_field(case.id, "financial.estimated_loss")
        assert field is not None
        assert field.human_modified is True
        assert field.source == "human"
        assert field.original_value == "GBP 320,000"
        assert field.modified_by == ACTOR

        # A re-run must not put the machine's reading back.
        await context.pipeline.run(case)
        await session.commit()
        assert case.estimated_loss_minor == 345_000_00

        # --- Claim creation ---------------------------------------------------
        result = await context.creation.create(case.id, actor=ACTOR)
        await session.commit()

        assert result.created is True
        claim = result.claim
        assert claim.reference.startswith("CLM-")
        assert claim.fnol_case_id == case.id
        assert claim.reserve_minor == 345_000_00
        assert case.status == FNOLStatus.CLAIM_CREATED
        assert case.claim_id == claim.id

        # The original notice survives conversion.
        assert case.source_body is not None
        # The attachment and the body document both survive conversion.
        assert len(await context.cases.list_documents(case.id)) == 2
        assert len(await context.cases.list_analyses(case.id)) >= 10

        # --- Triage and assignment --------------------------------------------
        triage = await context.claims.get_triage(claim.id)
        assert triage is not None
        assert triage.categories
        assert triage.recommended_route
        assert triage.reasoning

        assignment = await context.claims.get_assignment(claim.id)
        assert assignment is not None
        assert assignment.status in {"recommended", "unassigned"}

        # --- The claims queue ---------------------------------------------------
        rows, total = await context.claims.list_queue(search=claim.reference)
        assert total == 1
        assert rows[0].reference == claim.reference
        assert rows[0].status == ClaimStatus.CLASSIFIED

        # --- Audit ---------------------------------------------------------------
        events = await context.audit.history(case.id, related_ids=(claim.id,))
        event_types = {event.event_type for event in events}
        assert event_types >= {
            "fnol.created",
            "fnol.document_uploaded",
            "fnol.pipeline_completed",
            "fnol.field_changed",
            "claim.created",
            "claim.triage_completed",
        }
        # Machine actions are distinguishable from human ones.
        assert {event.actor_type for event in events} >= {"human", "ai"}

    async def test_creating_the_claim_twice_returns_the_first_one(self, session: object) -> None:
        context = build_context(session, None)
        case = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        first = await context.creation.create(case.id, actor=ACTOR)
        await session.commit()
        second = await context.creation.create(case.id, actor=ACTOR)
        await session.commit()

        assert first.created is True
        assert second.created is False
        assert first.claim.reference == second.claim.reference

        claims = (
            (await session.execute(select(Claim).where(Claim.fnol_case_id == case.id)))
            .scalars()
            .all()
        )
        assert len(claims) == 1

    async def test_redelivering_the_same_email_does_not_log_it_twice(self, session: object) -> None:
        context = build_context(session, None)
        message_id = f"<int-{uuid.uuid4()}@test>"

        first = await ingest_and_process(context, message_id=message_id)
        await session.commit()
        second = await ingest_and_process(context, message_id=message_id)
        await session.commit()

        assert first.id == second.id

    async def test_a_second_notice_of_the_same_loss_is_flagged_not_merged(
        self, session: object
    ) -> None:
        context = build_context(session, None)

        original = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()
        repeat = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        candidates = await context.cases.list_duplicates(repeat.id)
        assert [candidate.candidate_reference for candidate in candidates] == [original.reference]
        assert candidates[0].score >= settings.fnol.duplicate_similarity_threshold
        assert candidates[0].resolution == DuplicateResolution.UNRESOLVED
        assert repeat.status == FNOLStatus.POSSIBLE_DUPLICATE

        # Nothing was merged or deleted: both notices are still there.
        assert await context.cases.get(original.id) is not None

        # Claim creation is blocked until the officer decides.
        with pytest.raises(ClaimCreationBlocked) as blocked:
            await context.creation.create(repeat.id, actor=ACTOR)
        assert any(
            item["code"] == ExceptionCode.POSSIBLE_DUPLICATE.value
            for item in blocked.value.details["blockers"]
        )

        # Resolving it as a separate claim clears the block.
        await context.fnol.resolve_duplicate(
            repeat,
            candidates[0].id,
            resolution=DuplicateResolution.NEW_CLAIM,
            note="Two notifications, two separate bays.",
            actor=ACTOR,
        )
        await context.fnol.refresh_status(repeat)
        await session.commit()

        assert await context.creation.blockers(repeat) == []

    async def test_the_duplicate_scan_runs_once_and_travels_with_identification(
        self, session: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One scan per run, made by the stage that settles the policy.

        Both halves matter and they are the same guarantee. A repeat is scored on
        the policy above every other signal, so a scan made by a later stage would
        be reading a match nobody had re-checked; and a second scan would be the
        same few hundred comparisons, and the same writes, paid for twice.
        """
        scanned: list[str] = []
        detect = DuplicateDetectionService.detect

        async def counting(self: DuplicateDetectionService, case: object) -> object:
            scanned.append(case.reference)
            return await detect(self, case)

        monkeypatch.setattr(DuplicateDetectionService, "detect", counting)

        context = build_context(session, None)
        case = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        assert scanned == [case.reference]

        # And it is identification carrying it, not the pipeline: re-identifying
        # alone scans again, which is the whole point of moving it.
        await context.identification.identify(case)
        await session.commit()

        assert scanned == [case.reference, case.reference]

    async def test_re_identifying_a_notice_rescans_it_for_repeats(self, session: object) -> None:
        """The candidate list is refreshed by identification, not only by a full run.

        The first notice in was nobody's repeat when it was processed. Once the
        second arrives, re-identifying the first — one query and a few hundred
        comparisons, no model call — has to notice that, because an officer looking
        at a stale empty list has no way to tell it apart from a real answer.
        """
        context = build_context(session, None)

        first = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()
        assert await context.cases.list_duplicates(first.id) == []

        second = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        outcome = await context.identification.identify(first)
        await session.commit()

        assert [candidate.reference for candidate in outcome.duplicates.candidates] == [
            second.reference
        ]
        assert [record.candidate_reference for record in outcome.duplicates.records] == [
            second.reference
        ]
        # The rows the officer's screen reads agree with what the stage returned,
        # so nothing downstream has to go and ask again.
        persisted = await context.cases.list_duplicates(first.id)
        assert [row.candidate_reference for row in persisted] == [second.reference]
        assert outcome.duplicates.unresolved == list(persisted)

    async def test_a_clean_scan_reports_what_it_compared_not_just_an_empty_list(
        self, session: object
    ) -> None:
        """A clear result has to be distinguishable from a check that never ran.

        The first notice in is nobody's repeat, so the candidate list is empty —
        and an empty list is exactly what a stage that skipped the scan would also
        show. The count of what was compared is the difference, which is why it
        leaves the scan on the outcome and is written into the stored analysis
        rather than only into the log line.
        """
        context = build_context(session, None)

        first = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        outcome = await context.identification.identify(first)
        assert outcome.duplicates.candidates == []
        assert outcome.duplicates.as_dict()["compared"] == outcome.duplicates.compared

        # A second notice of the same loss gives the scan something to compare
        # against, so the count moves off zero while the notice stays flagged.
        second = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        repeat = await context.identification.identify(second)
        await session.commit()

        assert repeat.duplicates.compared >= 1
        assert len(repeat.duplicates.candidates) == 1

        stored = await context.cases.get_analysis(second.id, AnalysisKind.DUPLICATES)
        assert stored is not None
        assert stored.result["compared"] == repeat.duplicates.compared

    async def test_an_incomplete_notice_is_refused_with_a_checklist(self, session: object) -> None:
        context = build_context(session, None)

        notification = context.ingestion.from_email(
            IncomingEmail(
                sender="someone@example.com",
                recipient="fnol@carrier.com",
                subject="damage",
                message_id=f"<int-{uuid.uuid4()}@test>",
                body=(
                    "Something happened at one of our sites. I will call you. "
                    "Testfield Manufacturing will confirm."
                ),
            )
        )
        case, _ = await context.ingestion.ingest(notification, actor=ACTOR)
        await context.pipeline.run(case)
        await session.commit()

        assert case.status in {FNOLStatus.INCOMPLETE, FNOLStatus.POLICY_MATCH_REQUIRED}

        with pytest.raises(ClaimCreationBlocked) as blocked:
            await context.creation.create(case.id, actor=ACTOR)

        codes = {item["code"] for item in blocked.value.details["blockers"]}
        assert "policy_required" in codes
        assert "date_of_loss_required" in codes

        # Cleaned up by reference rather than by policy number, which is absent.
        await session.execute(delete(FNOLAIAnalysis).where(FNOLAIAnalysis.fnol_case_id == case.id))
        for model in (FNOLException, FNOLExtractedField, FNOLParty, FNOLPolicyMatch):
            await session.execute(delete(model).where(model.fnol_case_id == case.id))
        await session.execute(delete(AuditEvent).where(AuditEvent.entity_id == case.id))
        await session.execute(delete(FNOLCase).where(FNOLCase.id == case.id))
        await session.commit()

    async def test_reprocessing_reuses_the_extraction_while_the_inputs_hold_still(
        self, session: object
    ) -> None:
        context = build_context(session, None)
        case = await ingest_and_process(context, message_id=f"<int-{uuid.uuid4()}@test>")
        await session.commit()

        again = await context.pipeline.run(case)
        assert again.extraction_reused is True

        forced = await context.pipeline.run(case, force=True)
        assert forced.extraction_reused is False


class TestCatalogues:
    async def test_the_seeded_catastrophe_events_are_readable(self, session: object) -> None:
        context = build_context(session, None)
        events = await context.cat_events.list_all()
        assert isinstance(list(events), list)
        assert all(isinstance(event, CatEvent) for event in events)


class DeadProvider:
    """A provider that is configured, reachable in principle, and answering nothing.

    Distinct from passing `None`, which is the rest of this file's case and takes a
    different path entirely: with no provider the deterministic reader answers and
    the dataset path never starts. This is the production failure — an API key is
    set, the desk is running on model extraction, and the upstream is down. Every
    stage that calls a model has to degrade on its own, because the pipeline's
    promise is that a stage failure becomes an exception on the notice rather than
    a 500 on the request.
    """

    name = "dead"
    model = "dead-model"

    def __init__(self) -> None:
        self.calls = 0

    async def structured(self, **kwargs: object) -> object:
        self.calls += 1
        raise AIProviderError("Upstream is unreachable.", retryable=True)

    async def aclose(self) -> None:
        return None


class TestTheProviderIsDown:
    async def test_a_notice_still_lands_on_the_desk(self, session: object) -> None:
        """The whole point of the design, asserted once end to end.

        An unreachable model provider is a normal state of a claims desk — a
        rate limit, an expired key, an outage — and it must cost the notice its
        *values*, not its place in the queue. A notice that 500s is a notice
        nobody knows arrived.
        """
        provider = DeadProvider()
        context = build_context(session, provider)  # type: ignore[arg-type]

        case = await ingest_and_process(context, message_id=f"<dead-{uuid.uuid4()}@test>")
        await session.commit()

        # It was tried, and it was tried more than once — extraction, classification
        # and the summary each reach for a model.
        assert provider.calls > 0

        # The run finished. This is the assertion that matters: `failed` here would
        # mean an exception escaped a stage and the notice needs an operator.
        assert case.processing_state == "completed"
        assert case.processing_error is None

        # And it is on the desk, in a state that says what happened.
        assert case.status == FNOLStatus.AI_PROCESSING_FAILED.value
        codes = {
            exception.code
            for exception in await context.cases.list_exceptions(case.id, only_open=True)
        }
        assert ExceptionCode.AI_PROCESSING_FAILED.value in codes

        # The deterministic stages are untouched by the outage: they read the
        # notice's own columns and never call anything.
        identification = await context.cases.get_analysis(
            case.id, AnalysisKind.POLICY_IDENTIFICATION
        )
        assert identification is not None
        assert identification.status == "completed"
        completeness = await context.cases.get_analysis(case.id, AnalysisKind.COMPLETENESS)
        assert completeness is not None

        # The failure is recorded against the notice rather than only in a log, so
        # "why is this form empty" is answerable from the case.
        extraction = await context.cases.get_analysis(case.id, AnalysisKind.EXTRACTION)
        assert extraction is not None
        assert extraction.status == "failed"
        assert extraction.error

    async def test_the_notice_recovers_on_a_re_run_once_the_provider_returns(
        self, session: object
    ) -> None:
        """A failed read must not be cached as the answer.

        The run fingerprint is what stops a notice being re-read for nothing, and a
        failed run carrying one would make the outage permanent for every notice
        that arrived during it.
        """
        context = build_context(session, DeadProvider())  # type: ignore[arg-type]
        message_id = f"<recover-{uuid.uuid4()}@test>"
        case = await ingest_and_process(context, message_id=message_id)
        await session.commit()
        assert case.status == FNOLStatus.AI_PROCESSING_FAILED.value

        # The provider comes back. Nothing else about the notice has changed, so a
        # reused extraction here would leave it failed for ever.
        recovered = build_context(session, None)
        result = await recovered.pipeline.run(case)
        await session.commit()

        assert result.extraction_reused is False
        assert case.processing_state == "completed"
        assert case.status != FNOLStatus.AI_PROCESSING_FAILED.value
