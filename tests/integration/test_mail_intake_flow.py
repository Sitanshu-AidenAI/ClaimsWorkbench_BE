"""Mailbox intake against a real database.

Marked `integration` because it needs Postgres. The unit tests fake the session
and the repositories; this one proves the things a fake cannot: that the ledger
rows insert, that the unique constraints are what make a second poll a no-op,
and that an attachment arrives as an `FNOLDocument` with its bytes in the store.

Microsoft Graph is still stubbed — the point is the persistence, and a test that
needed a tenant would not run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.core.config import GraphSettings, settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import (
    FNOLChannel,
    MailAttachmentStatus,
    MailIntakeHealth,
    MailIntakeStatus,
    MailIntakeTrigger,
    NotificationKind,
    NotificationTone,
    ProcessingState,
)
from app.integrations.graph.messages import (
    FILE_ATTACHMENT_TYPE,
    GraphAttachmentMetadata,
    GraphMessage,
    GraphRecipient,
)
from app.models.audit import AuditEvent
from app.models.fnol import FNOLCase, FNOLDocument
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage, MailIntakeRun
from app.models.notification import Notification
from app.repositories.mail_intake import MailIntakeRepository
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.mail.health import MailIntakeHealthService
from app.services.mail.runner import build_mail_intake_service
from app.services.mail.watchdog import MailIntakeWatchdog

pytestmark = pytest.mark.integration

MAILBOX = "integration-claims@carrier.test"
GRAPH_ID = "AAMk-integration-1"
INTERNET_ID = "<integration-1@testbrokers.test>"
ESTIMATE = b"Item,Cost\nMake safe,12000.00\nReinstate finishing shop,180000.00\n"


class StubMailClient:
    """The mailbox, without Microsoft in it."""

    def __init__(self, message: GraphMessage, attachments: list[GraphAttachmentMetadata]) -> None:
        self.mailbox = MAILBOX
        self._message = message
        self._attachments = attachments
        self.marked_read: list[str] = []

    async def list_messages(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> list[GraphMessage]:
        if since is not None and self._message.received_at < since:
            return []
        return [self._message]

    async def folder_stats(self, folder: str | None = None) -> tuple[int, int]:
        return 1, 0

    async def list_attachments(self, message_id: str) -> list[GraphAttachmentMetadata]:
        return self._attachments

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        return ESTIMATE

    async def mark_as_read(self, message_id: str) -> bool:
        self.marked_read.append(message_id)
        return True

    async def move_message(self, message_id: str, destination: str) -> str | None:
        return None


def broker_message() -> GraphMessage:
    return GraphMessage(
        message_id=GRAPH_ID,
        internet_message_id=INTERNET_ID,
        conversation_id="conv-integration-1",
        subject="FNOL - Testfield Manufacturing - fire - TST-2026-0001",
        body=(
            "Policy number: TST-2026-0001\n"
            "Insured: Testfield Manufacturing Limited\n"
            "Cause: fire\n"
            "Estimated loss: GBP 320,000\n"
        ),
        body_is_html=False,
        received_at=datetime.now(UTC),
        has_attachments=True,
        is_read=False,
        sender=GraphRecipient(name="Alice Wren", address="claims@testbrokers.test"),
        to_recipients=[GraphRecipient(name="Claims", address=MAILBOX)],
    )


def estimate_attachment() -> GraphAttachmentMetadata:
    return GraphAttachmentMetadata(
        attachment_id="att-integration-1",
        name="estimate.csv",
        content_type="text/csv",
        size_bytes=len(ESTIMATE),
        is_inline=False,
        odata_type=FILE_ATTACHMENT_TYPE,
    )


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
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
        yield db
        await _cleanup(db)
        await db.commit()


async def _cleanup(db: object) -> None:
    """Remove this test's rows, children before parents."""
    case_ids = list(
        (
            await db.execute(
                select(MailIntakeMessage.fnol_case_id).where(MailIntakeMessage.mailbox == MAILBOX)
            )
        )
        .scalars()
        .all()
    )
    case_ids = [case_id for case_id in case_ids if case_id is not None]

    await db.execute(delete(MailIntakeRun).where(MailIntakeRun.mailbox == MAILBOX))
    await db.execute(delete(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX))
    if case_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(case_ids)))
        await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))


class TestMailboxIntakeAgainstPostgres:
    async def test_a_message_lands_as_a_queued_notice_with_its_document(
        self, session: object
    ) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]

        summary = await service.poll()

        assert (summary.ingested, summary.attachments_stored, summary.failed) == (1, 1, 0)
        assert client.marked_read == [GRAPH_ID]

        record = (
            (
                await session.execute(
                    select(MailIntakeMessage).where(MailIntakeMessage.graph_message_id == GRAPH_ID)
                )
            )
            .scalars()
            .one()
        )
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.internet_message_id == INTERNET_ID
        assert record.marked_read is True
        assert record.attempts == 1
        assert record.fnol_case_id is not None

        case = await session.get(FNOLCase, record.fnol_case_id)
        assert case is not None
        assert case.channel == FNOLChannel.BROKER_EMAIL
        # Queued, not processed: extraction is the next phase.
        assert case.processing_state == ProcessingState.QUEUED
        assert case.message_id == INTERNET_ID

        document = (
            (
                await session.execute(
                    select(FNOLDocument).where(FNOLDocument.fnol_case_id == case.id)
                )
            )
            .scalars()
            .one()
        )
        assert document.filename == "estimate.csv"
        assert document.source == "email_attachment"
        assert document.size_bytes == len(ESTIMATE)

        attachment = (
            (
                await session.execute(
                    select(MailIntakeAttachment).where(
                        MailIntakeAttachment.mail_intake_message_id == record.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert attachment.status == MailAttachmentStatus.STORED
        # The ledger points at the document the extraction phase will read.
        assert attachment.fnol_document_id == document.id
        assert attachment.checksum_sha256 == document.checksum_sha256

    async def test_a_second_poll_of_the_same_message_creates_nothing(self, session: object) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]

        first = await service.poll()
        second = await service.poll()

        assert (first.ingested, second.ingested) == (1, 0)
        assert second.duplicates == 1

        cases = (
            (await session.execute(select(FNOLCase).where(FNOLCase.message_id == INTERNET_ID)))
            .scalars()
            .all()
        )
        assert len(cases) == 1

        records = (
            (
                await session.execute(
                    select(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX)
                )
            )
            .scalars()
            .all()
        )
        assert len(records) == 1
        assert records[0].attempts == 1  # the second poll did no work

    async def test_a_redelivery_under_a_new_graph_id_is_recognised(self, session: object) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll()

        # Outlook mints a new id; the sending server's Message-ID does not change.
        redelivered = broker_message()
        redelivered.message_id = "AAMk-integration-2"
        resent = StubMailClient(redelivered, [estimate_attachment()])
        service = build_mail_intake_service(session, client=resent)  # type: ignore[arg-type]

        summary = await service.poll()

        assert (summary.ingested, summary.duplicates) == (0, 1)
        total = (
            await session.execute(
                select(MailIntakeMessage).where(MailIntakeMessage.mailbox == MAILBOX)
            )
        ).scalars()
        assert len(total.all()) == 1


class TestTheRunLedgerAgainstPostgres:
    """The record that makes a *missing* poll visible, proven where it has to work.

    The unit tests establish that `poll` writes a run row and that `evaluate`
    grades it. Neither can establish what this does: that the row survives a real
    insert, that the health service reads it back through a real query, and that
    the verdict flips on nothing but the passage of time.
    """

    async def test_a_poll_writes_a_run_row_that_the_health_check_reads_back(
        self, session: object
    ) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]

        summary = await service.poll(trigger=MailIntakeTrigger.SCHEDULE)
        assert summary.run_id is not None

        run = (
            (await session.execute(select(MailIntakeRun).where(MailIntakeRun.mailbox == MAILBOX)))
            .scalars()
            .one()
        )
        assert run.ok
        assert run.trigger == MailIntakeTrigger.SCHEDULE
        assert run.fetched == 1
        assert run.ingested == 1
        assert run.finished_at is not None

        graph = GraphSettings(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            shared_mailbox=MAILBOX,
            poll_enabled=True,
            poll_interval_seconds=300,
        )
        report = await MailIntakeHealthService(
            MailIntakeRepository(session),  # type: ignore[arg-type]
            config=graph,
        ).report()
        assert report.state == MailIntakeHealth.OK
        assert report.healthy

    async def test_the_same_ledger_reports_stale_once_the_run_is_old_enough(
        self, session: object
    ) -> None:
        """The outage, reproduced by moving one timestamp.

        Nothing about the mailbox, the credentials or the code changes between
        this test and the one above — only how long ago the last poll was. That is
        precisely the distinction that was previously invisible, and the reason
        the age of a row is the signal rather than anything a poll reports.
        """
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll(trigger=MailIntakeTrigger.SCHEDULE)

        run = (
            (await session.execute(select(MailIntakeRun).where(MailIntakeRun.mailbox == MAILBOX)))
            .scalars()
            .one()
        )
        run.started_at = datetime.now(UTC) - timedelta(days=6)
        await session.commit()

        graph = GraphSettings(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            shared_mailbox=MAILBOX,
            poll_enabled=True,
            poll_interval_seconds=300,
        )
        report = await MailIntakeHealthService(
            MailIntakeRepository(session),  # type: ignore[arg-type]
            config=graph,
        ).report()

        assert report.state == MailIntakeHealth.STALE
        assert not report.healthy
        assert "6 days" in report.detail

    async def test_a_manual_poll_is_recorded_as_manual_and_does_not_read_as_healthy(
        self, session: object
    ) -> None:
        """Pressing the button must not paper over a dead scheduler."""
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll(trigger=MailIntakeTrigger.MANUAL)

        graph = GraphSettings(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            shared_mailbox=MAILBOX,
            poll_enabled=True,
            poll_interval_seconds=300,
        )
        report = await MailIntakeHealthService(
            MailIntakeRepository(session),  # type: ignore[arg-type]
            config=graph,
        ).report()

        assert report.last_run_trigger == MailIntakeTrigger.MANUAL
        assert report.state == MailIntakeHealth.STALE
        assert not report.scheduled_run_seen

    async def test_run_records_past_their_retention_are_pruned(self, session: object) -> None:
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll()

        repository = MailIntakeRepository(session)  # type: ignore[arg-type]
        run = (
            (await session.execute(select(MailIntakeRun).where(MailIntakeRun.mailbox == MAILBOX)))
            .scalars()
            .one()
        )
        run.started_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()

        removed = await repository.prune_runs(before=datetime.now(UTC) - timedelta(days=14))
        await session.commit()

        assert removed >= 1
        assert await repository.latest_run(MAILBOX) is None


async def _unhealthy_notifications(session: object) -> list[Notification]:
    """Intake alarms raised about *this test's* mailbox only.

    Scoped on the mailbox in `context`, not on `kind` alone. The development
    database is shared with a live poller watching the real inbox, so a global
    query counts that mailbox's genuine alarms and a global delete would remove
    them — a test that quietly erases the very evidence this feature exists to
    produce.
    """
    rows = (
        (
            await session.execute(  # type: ignore[attr-defined]
                select(Notification).where(
                    Notification.kind == NotificationKind.MAIL_INTAKE_UNHEALTHY.value,
                    Notification.context["mailbox"].astext == MAILBOX,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _clear_unhealthy_notifications(session: object) -> None:
    for row in await _unhealthy_notifications(session):
        await session.delete(row)  # type: ignore[attr-defined]
    await session.commit()  # type: ignore[attr-defined]


class TestTheWatchdogAgainstPostgres:
    """The alarm that fires without being asked.

    `MailIntakeHealthService` answers the question; the watchdog is what asks it
    on a timer from the API process. These tests drive one tick directly rather
    than waiting for the loop — the loop is `asyncio.sleep` and a try/except, and
    what matters is what one tick does.
    """

    async def test_a_stale_ledger_puts_a_critical_notification_on_the_desk(
        self, session: object
    ) -> None:
        """The sentence that ends the six-day silence.

        A log line only reaches somebody who already suspects a problem. Nobody
        suspected one for six days, which is exactly why this has to land on the
        panel a handler is already looking at.
        """
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll(trigger=MailIntakeTrigger.SCHEDULE)

        run = (
            (await session.execute(select(MailIntakeRun).where(MailIntakeRun.mailbox == MAILBOX)))
            .scalars()
            .one()
        )
        run.started_at = datetime.now(UTC) - timedelta(days=6)
        await session.commit()

        watchdog = MailIntakeWatchdog(_stale_settings())
        report = await watchdog.check_once()

        assert report.state == MailIntakeHealth.STALE
        assert not report.healthy

        announced = await _unhealthy_notifications(session)
        assert len(announced) == 1
        assert announced[0].tone == NotificationTone.CRITICAL
        assert "6 days" in announced[0].body
        await _clear_unhealthy_notifications(session)

    async def test_a_continuing_outage_does_not_flood_the_panel(self, session: object) -> None:
        """One row per state per hour.

        A mailbox down since Tuesday must be visible every time the panel is
        opened, without having written thousands of rows to say the same thing.
        The hourly dedupe key is what buys both, and a panel that floods is a
        panel that gets ignored — which is how the alarm would end up as useless
        as the silence it replaced.
        """
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll(trigger=MailIntakeTrigger.SCHEDULE)

        run = (
            (await session.execute(select(MailIntakeRun).where(MailIntakeRun.mailbox == MAILBOX)))
            .scalars()
            .one()
        )
        run.started_at = datetime.now(UTC) - timedelta(days=6)
        await session.commit()

        watchdog = MailIntakeWatchdog(_stale_settings())
        for _ in range(5):
            await watchdog.check_once()

        announced = await _unhealthy_notifications(session)
        assert len(announced) == 1
        await _clear_unhealthy_notifications(session)

    async def test_a_healthy_poller_announces_nothing(self, session: object) -> None:
        """Silence is the correct output of a working system."""
        client = StubMailClient(broker_message(), [estimate_attachment()])
        service = build_mail_intake_service(session, client=client)  # type: ignore[arg-type]
        await service.poll(trigger=MailIntakeTrigger.SCHEDULE)

        watchdog = MailIntakeWatchdog(_stale_settings())
        report = await watchdog.check_once()

        assert report.state == MailIntakeHealth.OK
        announced = await _unhealthy_notifications(session)
        assert announced == []


def _stale_settings() -> object:
    """A copy of the real settings pointed at the test mailbox.

    The watchdog reads `settings.graph`, and the health verdict is computed
    against whatever mailbox that names. Overriding it here keeps the test's rows
    the only ones in scope.
    """
    config = settings.model_copy(deep=True)
    config.graph.tenant_id = "t"
    config.graph.client_id = "c"
    config.graph.client_secret = "s"
    config.graph.shared_mailbox = MAILBOX
    config.graph.poll_enabled = True
    config.graph.poll_interval_seconds = 300
    return config
