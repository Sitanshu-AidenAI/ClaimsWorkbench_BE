"""Mailbox to intake queue, end to end, against a real database.

`test_mail_intake_flow.py` proves collection: that a Graph message becomes a
ledger row, a notice and a stored attachment. This file starts where that one
stops and covers the seam onwards — the part that was specified but never tested
as one path:

    email arrives → parsed → attachment stored → notice queued
      → claimed off the queue → documents indexed → notice read
      → fields written → notice on the intake board
      → the desk told, at each step

Marked `integration` because the things worth proving here are the ones a fake
cannot: that the notice really reaches `processing_state = completed` through the
real pipeline, that the board query really returns it afterwards, that the unique
constraint on `notifications.dedupe_key` really makes a second poll silent, and
that each failure mode really lands somewhere a human can find it.

Microsoft Graph is stubbed and the model provider is deliberately absent, so the
deterministic reader runs. That is what makes this cheap enough for every commit,
and it is also the path a claims desk falls back to when the provider is down.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.api.deps.services import build_pipeline
from app.core.config import settings
from app.core.errors import ValidationError
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import (
    FNOLChannel,
    MailAttachmentStatus,
    MailIntakeStatus,
    NotificationKind,
    NotificationTone,
    ProcessingState,
)
from app.integrations.graph.errors import GraphError
from app.integrations.graph.messages import (
    FILE_ATTACHMENT_TYPE,
    GraphAttachmentMetadata,
    GraphMessage,
    GraphRecipient,
)
from app.models.audit import AuditEvent
from app.models.fnol import FNOLCase, FNOLDocument, FNOLExtractedField
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage
from app.models.notification import Notification
from app.repositories.fnol import FNOLRepository
from app.repositories.notification import NotificationRepository
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.mail.runner import build_mail_intake_service
from tests.fakes import FakeEmbeddingProvider, FakeVectorStore

pytestmark = pytest.mark.integration

MAILBOX = "pipeline-claims@carrier.test"
GRAPH_ID = "AAMk-pipeline-1"
INTERNET_ID = "<pipeline-1@testbrokers.test>"
SENDER = "claims@testbrokers.test"
SUBJECT = "FNOL - Brackwell Foods - escape of water - PIPE-2026-0001"

#: A broker's covering email. Written the way one actually reads — labelled lines
#: the deterministic reader can find, in prose a person would send — because the
#: point of this test is that a real notice comes out the far end, and a body of
#: `lorem ipsum` would prove only that the plumbing does not leak.
BODY = """Please find attached the first notification for our client.

Policy number: PIPE-2026-0001
Insured: Brackwell Foods Limited
Reported by: Marisa Kell
Contact email: m.kell@testbrokers.test

Date of loss: 06/08/2026
Location: Unit 4 Fenwick Trading Estate, Warrington WA5 1TT
Country: United Kingdom
Cause: escape of water
Description: A riser joint failed overnight on the second floor and water ran
down into the chilled store. Stock has been condemned and the store is closed.
Injuries: none
Estimated loss: GBP 148,000
"""

ESTIMATE = (
    b"Item,Cost\n"
    b"Make safe and dry out,18400.00\n"
    b"Reinstate chilled store,96000.00\n"
    b"Condemned stock,33600.00\n"
)


# ─────────────────────────────────────────────────────────────────────────────
# The mailbox, without Microsoft in it
# ─────────────────────────────────────────────────────────────────────────────


class StubMailClient:
    """A configurable stand-in for `GraphMailClient`.

    Every failure mode this file exercises is a behaviour of the mailbox rather
    than of the code under test, so they are all expressed here: attachments that
    will not list, bytes that will not download, a message that arrives twice.
    """

    def __init__(
        self,
        messages: list[GraphMessage],
        attachments: list[GraphAttachmentMetadata] | None = None,
        *,
        content: bytes = ESTIMATE,
        download_error: GraphError | None = None,
        list_attachments_error: GraphError | None = None,
    ) -> None:
        self.mailbox = MAILBOX
        self._messages = messages
        self._attachments = attachments or []
        self._content = content
        self._download_error = download_error
        self._list_attachments_error = list_attachments_error
        self.marked_read: list[str] = []

    async def list_messages(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> list[GraphMessage]:
        visible = [
            message for message in self._messages if since is None or message.received_at >= since
        ]
        return visible[:limit] if limit else visible

    async def folder_stats(self, folder: str | None = None) -> tuple[int, int]:
        return len(self._messages), 0

    async def list_attachments(self, message_id: str) -> list[GraphAttachmentMetadata]:
        if self._list_attachments_error is not None:
            raise self._list_attachments_error
        return self._attachments

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        if self._download_error is not None:
            raise self._download_error
        return self._content

    async def mark_as_read(self, message_id: str) -> bool:
        self.marked_read.append(message_id)
        return True

    async def move_message(self, message_id: str, destination: str) -> str | None:
        return None


def broker_message(
    *,
    graph_id: str = GRAPH_ID,
    internet_id: str | None = INTERNET_ID,
    subject: str | None = SUBJECT,
    body: str = BODY,
    has_attachments: bool = True,
) -> GraphMessage:
    return GraphMessage(
        message_id=graph_id,
        internet_message_id=internet_id,
        conversation_id="conv-pipeline-1",
        subject=subject,
        body=body,
        body_is_html=False,
        received_at=datetime.now(UTC) - timedelta(minutes=4),
        has_attachments=has_attachments,
        is_read=False,
        sender=GraphRecipient(name="Marisa Kell", address=SENDER),
        to_recipients=[GraphRecipient(name="Claims Intake", address=MAILBOX)],
    )


def estimate_attachment(
    *, name: str = "estimate.csv", size: int | None = None
) -> GraphAttachmentMetadata:
    return GraphAttachmentMetadata(
        attachment_id="att-pipeline-1",
        name=name,
        content_type="text/csv",
        size_bytes=size if size is not None else len(ESTIMATE),
        is_inline=False,
        odata_type=FILE_ATTACHMENT_TYPE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    """The engine, per test — the event loop is function-scoped, so this is too."""
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def _documents(tmp_path: Path) -> AsyncIterator[None]:
    """Attachment bytes go to a temporary directory, not to MinIO."""
    set_document_store(FilesystemDocumentStore(tmp_path))
    yield
    set_document_store(None)


@pytest.fixture
async def session() -> AsyncIterator[object]:
    factory = get_session_factory()
    async with factory() as db:
        # Swept before as well as after. Every message in this file reuses one
        # `Message-ID`, which is a unique column — so a single interrupted run
        # (a failed assertion whose teardown did not complete, `pytest -x`, a
        # cancelled run) leaves a row that makes every subsequent run of the whole
        # file fail on a constraint violation having nothing to do with the code
        # under test. Cleaning up front makes the suite self-healing against its
        # own accidents rather than needing a human with psql.
        await _cleanup(db)
        await db.commit()

        yield db

        await _cleanup(db)
        await db.commit()


async def _cleanup(db: object) -> None:
    """Remove this file's rows, children before parents.

    Keyed on the mailbox rather than on a reference prefix: the notice's reference
    is minted by the sequence and is not predictable, but every row this file
    creates is reachable from a ledger row naming `MAILBOX`.
    """
    case_ids = {
        case_id
        for case_id in (
            await db.execute(  # type: ignore[attr-defined]
                select(MailIntakeMessage.fnol_case_id).where(MailIntakeMessage.mailbox == MAILBOX)
            )
        )
        .scalars()
        .all()
        if case_id is not None
    }
    # Also by body, so a notice orphaned by an interrupted cleanup — the ledger row
    # deleted, the case not yet — is still found. Without this the sweep above can
    # only ever reach a case whose message survived to point at it.
    case_ids |= set(
        (
            await db.execute(  # type: ignore[attr-defined]
                select(FNOLCase.id).where(FNOLCase.source_body.ilike("%Brackwell Foods%"))
            )
        )
        .scalars()
        .all()
    )

    await db.execute(  # type: ignore[attr-defined]
        delete(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX)
    )
    if case_ids:
        # Notifications cascade with the case, but the audit events deliberately do
        # not carry a foreign key to it — so they are removed by hand or they
        # accumulate in a shared development database.
        await db.execute(  # type: ignore[attr-defined]
            delete(AuditEvent).where(AuditEvent.entity_id.in_(list(case_ids)))
        )
        await db.execute(  # type: ignore[attr-defined]
            delete(FNOLCase).where(FNOLCase.id.in_(list(case_ids)))
        )


def intake_service(db: object, client: StubMailClient) -> object:
    return build_mail_intake_service(db, client=client)  # type: ignore[arg-type]


def pipeline_for(db: object, *, provider: object = None) -> object:
    """The pipeline the beat task runs, built the way the worker builds it.

    Through `build_pipeline` rather than by hand: a test that assembled its own
    would stop proving anything the moment production wiring changed, and the
    notification service is exactly the kind of collaborator that gets added to
    one and forgotten in the other.
    """
    return build_pipeline(
        db,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        embeddings=FakeEmbeddingProvider(),
        vectors=FakeVectorStore(),
    )


async def notifications_for(db: object, case_id: uuid.UUID) -> list[Notification]:
    """Every notification raised about one notice, oldest first."""
    return list(
        (
            await db.execute(  # type: ignore[attr-defined]
                select(Notification)
                .where(Notification.fnol_case_id == case_id)
                .order_by(Notification.occurred_at, Notification.created_at)
            )
        )
        .scalars()
        .all()
    )


def kinds_of(notifications: list[Notification]) -> list[str]:
    return [item.kind for item in notifications]


# ─────────────────────────────────────────────────────────────────────────────
# The happy path
# ─────────────────────────────────────────────────────────────────────────────


class TestEmailToIntakeQueue:
    async def test_an_email_becomes_a_read_notice_on_the_intake_board(
        self, session: object
    ) -> None:
        """The whole specified flow, as one assertion sequence.

        Deliberately one test rather than eight. Each step's input is the previous
        step's committed output, and splitting them would mean either eight
        rebuilds of the same state or eight tests that quietly depend on execution
        order — and the thing being tested here *is* the ordering.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])

        # ── 1. The mail intake service detects the email ────────────────────
        summary = await intake_service(session, client).poll()

        assert (summary.fetched, summary.ingested, summary.failed) == (1, 1, 0)
        assert summary.attachments_stored == 1
        assert client.marked_read == [GRAPH_ID]

        record = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.fnol_case_id is not None
        case_id = record.fnol_case_id

        # ── 2. The body and the attachment were read correctly ──────────────
        case = await session.get(FNOLCase, case_id)  # type: ignore[attr-defined]
        assert case is not None
        assert case.channel == FNOLChannel.BROKER_EMAIL
        assert case.message_id == INTERNET_ID
        # The envelope reached the notice, not just the ledger.
        assert "Brackwell Foods" in (case.source_body or "")

        document = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLDocument).where(
                        FNOLDocument.fnol_case_id == case_id,
                        FNOLDocument.source == "email_attachment",
                    )
                )
            )
            .scalars()
            .one()
        )
        assert document.filename == "estimate.csv"
        assert document.size_bytes == len(ESTIMATE)
        # Read at collection time, so the pipeline has text to work from.
        assert "Reinstate chilled store" in (document.extracted_text or "")

        # ── 3. The notice is queued, and the desk has been told ─────────────
        assert case.processing_state == ProcessingState.QUEUED

        arrival = await notifications_for(session, case_id)
        assert kinds_of(arrival) == [NotificationKind.FNOL_EMAIL_RECEIVED.value]
        assert arrival[0].title == "New FNOL email received"
        assert arrival[0].body == "Processing has started."
        assert arrival[0].tone == NotificationTone.INFO
        assert arrival[0].fnol_reference == case.reference
        # The metadata the panel renders. Sender, subject and time are the three
        # things that tell a handler whether the arrival is theirs.
        assert arrival[0].context["sender_address"] == SENDER
        assert arrival[0].context["subject"] == SUBJECT
        assert arrival[0].context["mailbox"] == MAILBOX
        assert arrival[0].context["attachments_stored"] == 1
        assert arrival[0].context["status"] == "queued"
        # Announced as the moment the broker sent it, not the moment we found it.
        assert arrival[0].occurred_at == record.received_at

        # ── 4. The queue is claimed, and the pipeline runs ──────────────────
        cases = FNOLRepository(session)  # type: ignore[arg-type]
        claimed = await cases.claim_queued(limit=10)
        await session.commit()  # type: ignore[attr-defined]
        assert case_id in {item.id for item in claimed}

        result = await pipeline_for(session).run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        # ── 5. Extraction ran and the values landed ─────────────────────────
        assert result.error is None
        assert case.processing_state == ProcessingState.COMPLETED
        assert case.processing_completed_at is not None

        fields = list(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLExtractedField).where(FNOLExtractedField.fnol_case_id == case_id)
                )
            )
            .scalars()
            .all()
        )
        assert fields, "the notice was processed but no extracted fields were written"

        # Normalisation, not just capture: the policy number off a labelled line
        # and the money as minor units in a currency.
        assert case.policy_number == "PIPE-2026-0001"
        assert case.insured_name == "Brackwell Foods Limited"
        assert case.estimated_loss_minor == 148_000_00
        assert case.currency == "GBP"
        assert case.date_of_loss is not None

        # ── 6. The processed item appears on the intake board ───────────────
        # The same query `GET /fnol/board` runs, with the same default ordering, so
        # this asserts the notice is on the queue an officer actually looks at
        # rather than merely present in the table behind it.
        board, total = await cases.list_cases(order="severity", limit=50)
        assert case.reference in {row.reference for row in board}, (
            "the processed notice is not on the intake queue the board endpoint serves"
        )
        assert total >= 1

        # ── 7. The desk was told how it went ───────────────────────────────
        after = await notifications_for(session, case_id)
        assert kinds_of(after) == [
            NotificationKind.FNOL_EMAIL_RECEIVED.value,
            NotificationKind.FNOL_PROCESSING_STARTED.value,
            NotificationKind.FNOL_PROCESSING_SUCCEEDED.value,
        ]

        succeeded = after[-1]
        assert "processed successfully and added to the intake queue" in succeeded.body
        assert succeeded.fnol_reference == case.reference
        assert succeeded.context["status"] == result.status.value
        # Tone follows whether anything is blocking, not whether the run finished:
        # a green tick on a notice nobody can progress would say the opposite of
        # what the handler needs to know.
        expected_tone = (
            NotificationTone.WARNING if result.exceptions_raised else NotificationTone.SUCCESS
        )
        assert succeeded.tone == expected_tone

    async def test_the_panel_is_per_person_and_the_badge_clears_for_one_reader(
        self, session: object
    ) -> None:
        """Two officers, one desk-wide arrival, two independent badges.

        The property that makes a shared claims mailbox usable, and the reason the
        read marker is its own table: one officer acknowledging an arrival must not
        blank it for everybody else.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        await intake_service(session, client).poll()

        notifications = NotificationRepository(session)  # type: ignore[arg-type]
        alice, bob = "subject-alice", "subject-bob"

        assert await notifications.unread_count(alice) >= 1
        before_bob = await notifications.unread_count(bob)

        rows, _ = await notifications.list_for(alice, limit=50)
        mine = [row.id for row in rows if row.fnol_reference is not None]
        assert await notifications.mark_read(mine, alice) == len(mine)
        await session.commit()  # type: ignore[attr-defined]

        # Marking is idempotent: the panel fires it on every open without
        # tracking what it has already sent.
        assert await notifications.mark_read(mine, alice) == 0

        assert await notifications.unread_count(alice) == 0
        assert await notifications.unread_count(bob) == before_bob

        # And the row itself still reports unread *for Bob*.
        read_for_alice = await notifications.read_subjects(mine, alice)
        read_for_bob = await notifications.read_subjects(mine, bob)
        assert read_for_alice == set(mine)
        assert read_for_bob == set()

    async def test_clearing_the_badge_marks_everything_outstanding_for_one_reader(
        self, session: object
    ) -> None:
        """`mark_all_read` selects its own candidates in one statement.

        Worth its own test because the alternative — fetch the ids, send them back
        — has a window in which an arrival can be marked read without ever having
        been shown, and because the statement is an `INSERT ... FROM SELECT` whose
        correctness is not obvious by reading it.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        await intake_service(session, client).poll()

        notifications = NotificationRepository(session)  # type: ignore[arg-type]
        carol, dave = "subject-carol", "subject-dave"

        outstanding = await notifications.unread_count(carol)
        assert outstanding >= 1

        assert await notifications.mark_all_read(carol) == outstanding
        await session.commit()  # type: ignore[attr-defined]

        assert await notifications.unread_count(carol) == 0
        # Idempotent, and scoped: Dave's badge is untouched.
        assert await notifications.mark_all_read(carol) == 0
        assert await notifications.unread_count(dave) == outstanding


# ─────────────────────────────────────────────────────────────────────────────
# Failure modes
# ─────────────────────────────────────────────────────────────────────────────


class TestDuplicateDelivery:
    async def test_a_second_poll_creates_no_second_notice_and_no_second_notification(
        self, session: object
    ) -> None:
        """The property the whole ledger exists for, now including the panel.

        A mailbox with `mark_as_read=false` re-lists every message on every poll.
        Before the panel that cost a query; the risk introduced by announcing
        arrivals is that it starts costing a handler a notification every five
        minutes forever.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        service = intake_service(session, client)

        first = await service.poll()
        second = await service.poll()

        assert (first.ingested, second.ingested) == (1, 0)
        assert second.duplicates == 1

        case_id = first_case_id = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeMessage.fnol_case_id).where(
                        MailIntakeMessage.mailbox == MAILBOX
                    )
                )
            )
            .scalars()
            .one()
        )
        assert first_case_id is not None

        assert kinds_of(await notifications_for(session, case_id)) == [
            NotificationKind.FNOL_EMAIL_RECEIVED.value
        ]

    async def test_a_redelivery_under_a_new_graph_id_announces_nothing_new(
        self, session: object
    ) -> None:
        """Outlook mints a new id; the sending server's `Message-ID` does not.

        The notice is deduplicated on the `Message-ID`, so no second notice
        appears — and because the arrival notification is keyed on the *Graph* id,
        this is the one case where the two keys disagree. The assertion below pins
        which one wins: nothing new is announced, because nothing new arrived.
        """
        await intake_service(
            session, StubMailClient([broker_message()], [estimate_attachment()])
        ).poll()

        redelivered = broker_message(graph_id="AAMk-pipeline-2")
        summary = await intake_service(
            session, StubMailClient([redelivered], [estimate_attachment()])
        ).poll()

        assert (summary.ingested, summary.duplicates) == (0, 1)

        cases = list(
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)
                )
            )
            .scalars()
            .all()
        )
        assert len(cases) == 1

        # One arrival announced, not two — the re-delivery reached the ledger's
        # `PROCESSED` short-circuit before any notification could be written.
        panel = await notifications_for(session, cases[0].id)
        assert kinds_of(panel) == [NotificationKind.FNOL_EMAIL_RECEIVED.value]


class TestMalformedAndMissingContent:
    async def test_an_envelope_with_no_subject_or_body_still_becomes_a_notice(
        self, session: object
    ) -> None:
        """A malformed email is a claim notification too.

        The one thing intake must never do is drop a message because it was badly
        formed: a broker who sent an empty email still sent it, and an officer
        asked "where is it" must be able to find something. So this asserts the
        notice exists and is *findable*, not that it is complete.
        """
        message = broker_message(subject=None, body="", has_attachments=False)
        summary = await intake_service(session, StubMailClient([message])).poll()

        assert (summary.ingested, summary.failed) == (1, 0)

        record = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.fnol_case_id is not None

        # Announced, and honestly: no attachments, and the panel says so rather
        # than implying a survey report is on its way.
        panel = await notifications_for(session, record.fnol_case_id)
        assert kinds_of(panel) == [NotificationKind.FNOL_EMAIL_RECEIVED.value]
        assert panel[0].context["attachments_stored"] == 0
        assert panel[0].context["subject"] is None

    async def test_an_attachment_that_will_not_download_does_not_lose_the_notice(
        self, session: object
    ) -> None:
        """One file failing is not a reason to lose the email that carried it."""
        client = StubMailClient(
            [broker_message()],
            [estimate_attachment()],
            download_error=GraphError("Attachment content is unavailable.", status_code=502),
        )
        summary = await intake_service(session, client).poll()

        assert summary.ingested == 1
        assert summary.attachments_failed == 1
        assert summary.attachments_stored == 0

        record = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.fnol_case_id is not None

        # The refusal is on the record, with a sentence fit to show a human.
        attachment = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeAttachment).where(
                        MailIntakeAttachment.mail_intake_message_id == record.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert attachment.status == MailAttachmentStatus.FAILED
        assert attachment.detail
        assert attachment.fnol_document_id is None

        panel = await notifications_for(session, record.fnol_case_id)
        assert panel[0].context["attachments_stored"] == 0
        assert panel[0].context["attachments_skipped"] == 1

    async def test_an_oversized_attachment_is_refused_before_it_is_downloaded(
        self, session: object
    ) -> None:
        """The size gate reads Graph's declared size, so nothing is transferred.

        `download_error` is armed deliberately: if the gate ever starts fetching
        first and refusing second, this test fails loudly rather than passing more
        slowly.
        """
        oversized = estimate_attachment(
            name="drone-survey.mp4", size=settings.fnol.max_document_bytes + 1
        )
        client = StubMailClient(
            [broker_message()],
            [oversized],
            download_error=GraphError("This should never have been requested.", status_code=500),
        )

        summary = await intake_service(session, client).poll()

        assert (summary.ingested, summary.attachments_skipped) == (1, 1)
        assert summary.attachments_failed == 0


class TestPipelineFailures:
    async def test_a_pipeline_that_raises_leaves_a_failure_notification(
        self, session: object
    ) -> None:
        """The `FNOL processing failed. Review required.` path.

        Provoked by breaking a collaborator rather than by a flag, because the
        contract under test is "whatever goes wrong in a stage, the handler is
        told" — and a test that asked the pipeline to pretend to fail would not
        cover the case where something genuinely unexpected does.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        await intake_service(session, client).poll()

        case = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)
                )
            )
            .scalars()
            .one()
        )

        pipeline = pipeline_for(session)

        class Broken:
            async def classify(self, **kwargs: object) -> object:
                raise RuntimeError("the classifier is unreachable")

        pipeline._classification = Broken()  # type: ignore[attr-defined]

        result = await pipeline.run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        # The pipeline reports rather than raises — a failed stage is a normal
        # state of a claims desk, not a 500.
        assert result.error is not None
        assert case.processing_state == ProcessingState.FAILED

        panel = await notifications_for(session, case.id)
        assert kinds_of(panel) == [
            NotificationKind.FNOL_EMAIL_RECEIVED.value,
            NotificationKind.FNOL_PROCESSING_STARTED.value,
            NotificationKind.FNOL_PROCESSING_FAILED.value,
        ]

        failure = panel[-1]
        assert failure.body == "FNOL processing failed. Review required."
        assert failure.tone == NotificationTone.CRITICAL
        assert failure.fnol_reference == case.reference
        # The cause is named, clipped, and free of a stack trace.
        assert "RuntimeError" in (failure.context["error"] or "")
        assert len(failure.context["error"] or "") <= 400

    async def test_one_run_announces_itself_once_however_often_the_task_is_delivered(
        self, session: object
    ) -> None:
        """Celery at-least-once delivery must not become a handler's problem.

        The dedupe key for a pipeline event is the case *plus the instant the run
        started*, so re-running the same run is silent while a genuine second run
        — which is what an officer pressing "reprocess" causes — is announced.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        await intake_service(session, client).poll()

        case = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)
                )
            )
            .scalars()
            .one()
        )

        pipeline = pipeline_for(session)
        await pipeline.run(case)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        first_pass = kinds_of(await notifications_for(session, case.id))
        assert first_pass.count(NotificationKind.FNOL_PROCESSING_SUCCEEDED.value) == 1

        # A second genuine run. It gets its own pair, because the handler was told
        # about the first one and silence here would leave them looking at a stale
        # outcome.
        await pipeline.run(case, force=True)  # type: ignore[attr-defined]
        await session.commit()  # type: ignore[attr-defined]

        second_pass = kinds_of(await notifications_for(session, case.id))
        assert second_pass.count(NotificationKind.FNOL_PROCESSING_STARTED.value) == 2
        assert second_pass.count(NotificationKind.FNOL_PROCESSING_SUCCEEDED.value) == 2
        # And exactly one arrival, still: the email only came once.
        assert second_pass.count(NotificationKind.FNOL_EMAIL_RECEIVED.value) == 1


class TestIngestionFailure:
    async def test_a_notice_that_cannot_be_created_is_recorded_not_lost(
        self, session: object
    ) -> None:
        """Ingestion failing is the one case with no notice to announce.

        So the assertion is about the *ledger*: the attempt count, the status and
        the error must all land, because that row is the only thing that can answer
        "the broker says they sent it — where is it?". A notification would have
        nowhere to point, and inventing one would be worse than none.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        service = intake_service(session, client)

        class Refusing:
            def from_email(self, email: object) -> object:
                raise ValidationError("This envelope cannot become a notification.")

        service._ingestion = Refusing()  # type: ignore[attr-defined]

        summary = await service.poll()

        assert (summary.ingested, summary.failed) == (0, 1)

        record = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.status == MailIntakeStatus.FAILED
        assert record.attempts == 1
        assert record.last_error is not None
        assert "cannot become a notification" in record.last_error
        assert record.fnol_case_id is None

        # Nothing was announced, because nothing arrived that a handler could open.
        assert (
            await session.execute(  # type: ignore[attr-defined]
                select(Notification).where(
                    Notification.dedupe_key
                    == f"{NotificationKind.FNOL_EMAIL_RECEIVED.value}:{GRAPH_ID}"
                )
            )
        ).scalars().first() is None

    async def test_a_message_is_abandoned_after_the_configured_attempts(
        self, session: object
    ) -> None:
        """Retrying forever is how a poison message stops a mailbox.

        The ledger's attempt count is the bound, and this is the assertion that it
        is actually consulted — without it a permanently unparseable email would be
        retried every five minutes for the life of the deployment.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])

        class Refusing:
            def from_email(self, email: object) -> object:
                raise ValidationError("Still unparseable.")

        for _ in range(settings.graph.max_attempts):
            service = intake_service(session, client)
            service._ingestion = Refusing()  # type: ignore[attr-defined]
            await service.poll()

        record = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.attempts == settings.graph.max_attempts

        # One more poll: left alone, and reported as abandoned rather than retried.
        service = intake_service(session, client)
        service._ingestion = Refusing()  # type: ignore[attr-defined]
        summary = await service.poll()

        assert summary.abandoned == 1
        assert summary.failed == 0

        await session.refresh(record)  # type: ignore[attr-defined]
        assert record.attempts == settings.graph.max_attempts


class TestTheApiTheBrowserCalls:
    """The panel over real HTTP, against real rows.

    The repository tests above prove the queries; this proves the *wire shape* the
    browser is coded against — field names, the per-caller `read` flag, and an
    `unread` total that is not bounded by the page. A mismatch here is a frontend
    that renders blanks, which no repository test can catch.
    """

    async def test_the_panel_endpoint_serves_the_arrival_and_clears_on_acknowledgement(
        self, session: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from prometheus_client import CollectorRegistry

        import app.main as app_main
        from app.api.deps.auth import get_current_principal, get_verifier
        from app.api.deps.db import get_session
        from app.core.security import Principal

        client = StubMailClient([broker_message()], [estimate_attachment()])
        await intake_service(session, client).poll()

        case = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)
                )
            )
            .scalars()
            .one()
        )

        async def _noop(*_args: object, **_kwargs: object) -> None:
            return None

        # The lifespan's own startup would open a second engine on this loop; the
        # test already has one, and the session override below is what the routes
        # actually use.
        for target in (
            "init_engine",
            "dispose_engine",
            "init_pool",
            "close_pool",
            "init_redis",
            "close_redis",
        ):
            monkeypatch.setattr(app_main, target, _noop)

        application: FastAPI = app_main.create_app(metrics_registry=CollectorRegistry())

        async def _session_override() -> AsyncIterator[object]:
            yield session

        principal = Principal(
            subject="subject-http-reader",
            username="officer",
            realm_roles=frozenset({"fnol-officer"}),
        )
        application.dependency_overrides[get_session] = _session_override
        application.dependency_overrides[get_verifier] = lambda: None
        application.dependency_overrides[get_current_principal] = lambda: principal

        try:
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                listing = await http.get("/api/v1/notifications", params={"case_id": str(case.id)})
                assert listing.status_code == 200
                payload = listing.json()

                assert payload["total"] == 1
                assert payload["unread"] >= 1
                row = payload["items"][0]
                assert row["kind"] == NotificationKind.FNOL_EMAIL_RECEIVED.value
                assert row["title"] == "New FNOL email received"
                assert row["body"] == "Processing has started."
                assert row["tone"] == NotificationTone.INFO.value
                assert row["fnol_reference"] == case.reference
                assert row["read"] is False
                # The metadata the dropdown renders under the title.
                assert row["context"]["sender_address"] == SENDER
                assert row["context"]["subject"] == SUBJECT

                acknowledged = await http.post(
                    "/api/v1/notifications/read", json={"notification_ids": [row["id"]]}
                )
                assert acknowledged.status_code == 200
                assert acknowledged.json()["marked"] == 1

                again = await http.get("/api/v1/notifications", params={"case_id": str(case.id)})
                assert again.json()["items"][0]["read"] is True

                # Repeating it is a no-op rather than a 500 — the panel fires this
                # on every open without tracking what it has already sent.
                repeat = await http.post(
                    "/api/v1/notifications/read", json={"notification_ids": [row["id"]]}
                )
                assert repeat.status_code == 200
                assert repeat.json()["marked"] == 0
        finally:
            application.dependency_overrides.clear()


class TestDeletionSweepsThePanel:
    async def test_deleting_a_notice_removes_the_notifications_that_named_it(
        self, session: object
    ) -> None:
        """A panel row that opens onto a 404 is a lie about what happened.

        The same argument the mailbox ledger makes, one step further on. Asserted
        here rather than in the deletion test because this is the file that knows
        how notifications come to exist in the first place.
        """
        client = StubMailClient([broker_message()], [estimate_attachment()])
        await intake_service(session, client).poll()

        case = (
            (
                await session.execute(  # type: ignore[attr-defined]
                    select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)
                )
            )
            .scalars()
            .one()
        )
        case_id = case.id
        assert await notifications_for(session, case_id)

        await session.execute(  # type: ignore[attr-defined]
            delete(FNOLCase).where(FNOLCase.id == case_id)
        )
        await session.commit()  # type: ignore[attr-defined]

        assert await notifications_for(session, case_id) == []
