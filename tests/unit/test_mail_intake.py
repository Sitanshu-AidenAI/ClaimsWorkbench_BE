"""Mailbox intake orchestration: idempotency, attachments, and partial failure.

The Graph client, the session and the repositories are faked; the FNOL
ingestion mapping and the document validation are the real ones, because those
are the two places where a wrong assumption would show up in production rather
than in a fake.

The fake session is transactional on purpose — `rollback()` really does discard
what was added since the last commit — since the whole point of the failure path
is what survives a rollback.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps.services import (
    MailIntakeContext,
    build_mail_intake_context,
    get_mail_client_dependency,
)
from app.core.config import FNOLSettings, GraphSettings
from app.domain.enums import (
    FNOLChannel,
    MailAttachmentStatus,
    MailIntakeStatus,
    MailIntakeTrigger,
    ProcessingState,
)
from app.integrations.graph.client import GraphNotConfiguredError
from app.integrations.graph.errors import GraphError
from app.integrations.graph.messages import (
    FILE_ATTACHMENT_TYPE,
    GraphAttachmentMetadata,
    GraphMessage,
    GraphRecipient,
)
from app.models.fnol import FNOLCase, FNOLDocument
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage, MailIntakeRun
from app.services.documents.validation import validate_upload
from app.services.fnol.ingestion import FNOLIngestionService
from app.services.mail.intake import INTAKE_ACTOR, MailIntakeService

MAILBOX = "claims@carrier.test"
PDF = b"%PDF-1.7\nSurvey report: estimated loss GBP 128,000\n"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def a_message(
    *,
    message_id: str = "AAMk-1",
    internet_message_id: str | None = "<abc@broker.test>",
    has_attachments: bool = False,
) -> GraphMessage:
    return GraphMessage(
        message_id=message_id,
        internet_message_id=internet_message_id,
        conversation_id="conv-1",
        subject="FNOL — flood at Unit 4, policy POL-2026-0041",
        body="<p>Water ingress overnight. Estimated loss GBP 128,000.</p>",
        body_is_html=True,
        received_at=datetime(2026, 8, 11, 9, 14, tzinfo=UTC),
        has_attachments=has_attachments,
        is_read=False,
        sender=GraphRecipient(name="Priya Raman", address="priya@broker.test"),
        to_recipients=[GraphRecipient(name="Claims", address=MAILBOX)],
        cc_recipients=[],
        body_preview="Water ingress overnight",
    )


def an_attachment(
    *,
    attachment_id: str = "att-1",
    name: str = "survey-report.pdf",
    size: int = len(PDF),
    inline: bool = False,
    odata_type: str = FILE_ATTACHMENT_TYPE,
) -> GraphAttachmentMetadata:
    return GraphAttachmentMetadata(
        attachment_id=attachment_id,
        name=name,
        content_type="application/pdf",
        size_bytes=size,
        is_inline=inline,
        odata_type=odata_type,
    )


class FakeMailClient:
    """A mailbox in a dictionary, with the two write calls recorded."""

    def __init__(
        self,
        messages: list[GraphMessage],
        attachments: dict[str, list[GraphAttachmentMetadata]] | None = None,
        *,
        content: bytes = PDF,
        download_error: Exception | None = None,
        mark_error: Exception | None = None,
    ) -> None:
        self.mailbox = MAILBOX
        self._messages = messages
        self._attachments = attachments or {}
        self._content = content
        self._download_error = download_error
        self._mark_error = mark_error
        self.downloaded: list[str] = []
        self.marked_read: list[str] = []
        self.moved: list[tuple[str, str]] = []
        #: Appended to by the fake session too, so ordering can be asserted.
        self.journal: list[str] = []
        #: The watermark each poll swept from, so the caller's side can be asserted.
        self.since_seen: list[datetime | None] = []
        #: What `folder_stats` reports. Defaults to agreeing with the ledger.
        self.folder_total: int | None = None
        #: Unread mail the folder claims to hold, for the blind-sweep check.
        self.folder_unread = 0
        self.folder_stats_error: Exception | None = None

    async def list_messages(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> list[GraphMessage]:
        self.since_seen.append(since)
        visible = [
            message for message in self._messages if since is None or message.received_at >= since
        ]
        return visible[: limit or len(visible)]

    async def folder_stats(self, folder: str | None = None) -> tuple[int, int]:
        if self.folder_stats_error is not None:
            raise self.folder_stats_error
        total = self.folder_total if self.folder_total is not None else len(self._messages)
        return total, self.folder_unread

    async def list_attachments(self, message_id: str) -> list[GraphAttachmentMetadata]:
        return self._attachments.get(message_id, [])

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        if self._download_error is not None:
            raise self._download_error
        self.downloaded.append(attachment_id)
        return self._content

    async def mark_as_read(self, message_id: str) -> bool:
        if self._mark_error is not None:
            raise self._mark_error
        self.marked_read.append(message_id)
        self.journal.append("mark_read")
        return True

    async def move_message(self, message_id: str, destination: str) -> str | None:
        self.moved.append((message_id, destination))
        return "AAMk-moved"


class FakeMailRepository:
    """The ledger, in memory, with committed and pending rows kept apart."""

    def __init__(self) -> None:
        self.committed: list[MailIntakeMessage] = []
        self.pending: list[MailIntakeMessage] = []
        #: Poll records, in the order they were written. The health check reads
        #: these; the intake service writes one per poll including the failures.
        self.runs: list[MailIntakeRun] = []

    @property
    def rows(self) -> list[MailIntakeMessage]:
        return [*self.committed, *self.pending]

    def seed(self, record: MailIntakeMessage) -> MailIntakeMessage:
        self.committed.append(record)
        return record

    def add(self, message: MailIntakeMessage) -> MailIntakeMessage:
        self.pending.append(message)
        return message

    async def flush(self) -> None:
        # The database assigns the id on flush, and the service reads it
        # immediately afterwards to key attachments off.
        for row in self.pending:
            if row.id is None:
                row.id = uuid.uuid4()

    async def newest_received_at(self, mailbox: str) -> datetime | None:
        stamps = [row.received_at for row in self.rows if row.mailbox == mailbox]
        return max(stamps) if stamps else None

    async def count_for_mailbox(self, mailbox: str) -> int:
        return sum(1 for row in self.rows if row.mailbox == mailbox)

    async def count_recorded(
        self, *, graph_message_ids: Sequence[str], internet_message_ids: Sequence[str]
    ) -> int:
        graph = set(graph_message_ids)
        internet = set(internet_message_ids)
        return sum(
            1
            for row in self.rows
            if row.graph_message_id in graph
            or (row.internet_message_id is not None and row.internet_message_id in internet)
        )

    def add_run(self, run: MailIntakeRun) -> MailIntakeRun:
        if run.id is None:
            run.id = uuid.uuid4()
        self.runs.append(run)
        return run

    async def latest_run(self, mailbox: str | None = None) -> MailIntakeRun | None:
        rows = [row for row in self.runs if mailbox is None or row.mailbox == mailbox]
        return max(rows, key=lambda row: row.started_at) if rows else None

    async def latest_successful_run(self, mailbox: str | None = None) -> MailIntakeRun | None:
        rows = [row for row in self.runs if row.ok and (mailbox is None or row.mailbox == mailbox)]
        return max(rows, key=lambda row: row.started_at) if rows else None

    async def list_runs(
        self, *, mailbox: str | None = None, limit: int = 20
    ) -> list[MailIntakeRun]:
        rows = [row for row in self.runs if mailbox is None or row.mailbox == mailbox]
        return sorted(rows, key=lambda row: row.started_at, reverse=True)[:limit]

    def add_attachment(self, attachment: MailIntakeAttachment) -> MailIntakeAttachment:
        record = next(row for row in self.rows if row.id == attachment.mail_intake_message_id)
        record.attachments.append(attachment)
        return attachment

    async def find_attachment(
        self, message_id: uuid.UUID, graph_attachment_id: str
    ) -> MailIntakeAttachment | None:
        record = next((row for row in self.rows if row.id == message_id), None)
        if record is None:
            return None
        return next(
            (
                item
                for item in record.attachments
                if item.graph_attachment_id == graph_attachment_id
            ),
            None,
        )

    def on_commit(self) -> None:
        self.committed.extend(self.pending)
        self.pending.clear()

    def on_rollback(self) -> None:
        self.pending.clear()

    async def find_by_graph_id(self, graph_message_id: str) -> MailIntakeMessage | None:
        return next((row for row in self.rows if row.graph_message_id == graph_message_id), None)

    async def find_by_internet_message_id(self, value: str) -> MailIntakeMessage | None:
        return next((row for row in self.rows if row.internet_message_id == value), None)

    async def find_existing(
        self, *, graph_message_id: str, internet_message_id: str | None
    ) -> MailIntakeMessage | None:
        found = await self.find_by_graph_id(graph_message_id)
        if found is not None:
            return found
        if internet_message_id:
            return await self.find_by_internet_message_id(internet_message_id)
        return None


class FakeSession:
    def __init__(self, repository: FakeMailRepository, journal: list[str]) -> None:
        self._repository = repository
        self._journal = journal
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1
        self._journal.append("commit")
        self._repository.on_commit()

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._repository.on_rollback()


@dataclass
class FakeIngestion:
    """The real envelope mapping, with a stubbed case factory behind it."""

    existing: FNOLCase | None = None
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    _counter: int = 0

    from_email = staticmethod(FNOLIngestionService.from_email)

    async def ingest(self, notification: object, *, actor: str) -> tuple[FNOLCase, bool]:
        if self.error is not None:
            raise self.error
        self.calls.append(actor)
        if self.existing is not None:
            return self.existing, False
        self._counter += 1
        case = FNOLCase(
            reference=f"FNOL-2026-00000{self._counter}",
            channel=FNOLChannel.BROKER_EMAIL.value,
            received_at=datetime.now(UTC),
        )
        case.id = uuid.uuid4()
        return case, True


class FakeFNOLService:
    """Stands in for `FNOLService`, but runs the real upload validation."""

    def __init__(self, *, max_bytes: int = 25 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self.attached: list[str] = []
        self.queued: list[str] = []

    async def attach_document(
        self,
        case: FNOLCase,
        *,
        filename: str,
        content: bytes,
        content_type: str | None,
        source: str,
        actor: str,
    ) -> FNOLDocument:
        # The same refusal a browser upload would get.
        safe_name, resolved = validate_upload(
            filename, content, max_bytes=self._max_bytes, declared_content_type=content_type
        )
        self.attached.append(safe_name)
        document = FNOLDocument(
            fnol_case_id=case.id,
            filename=safe_name,
            content_type=resolved,
            size_bytes=len(content),
            storage_key=f"fnol/{case.reference}/{safe_name}",
            checksum_sha256="a" * 64,
            source=source,
            uploaded_by=actor,
        )
        document.id = uuid.uuid4()
        return document

    def mark_queued(self, case: FNOLCase) -> None:
        case.processing_state = ProcessingState.QUEUED
        self.queued.append(case.reference)


@dataclass
class Harness:
    service: MailIntakeService
    client: FakeMailClient
    repository: FakeMailRepository
    session: FakeSession
    ingestion: FakeIngestion
    fnol: FakeFNOLService

    @property
    def records(self) -> list[MailIntakeMessage]:
        return self.repository.rows


def build(
    messages: Sequence[GraphMessage],
    attachments: dict[str, list[GraphAttachmentMetadata]] | None = None,
    *,
    graph: GraphSettings | None = None,
    fnol_config: FNOLSettings | None = None,
    ingestion: FakeIngestion | None = None,
    seed: MailIntakeMessage | None = None,
    **client_kwargs: object,
) -> Harness:
    client = FakeMailClient(list(messages), attachments, **client_kwargs)  # type: ignore[arg-type]
    repository = FakeMailRepository()
    if seed is not None:
        repository.seed(seed)
    session = FakeSession(repository, client.journal)
    ingestion = ingestion or FakeIngestion()
    fnol = FakeFNOLService()

    service = MailIntakeService(
        session=session,  # type: ignore[arg-type]
        messages=repository,  # type: ignore[arg-type]
        ingestion=ingestion,  # type: ignore[arg-type]
        fnol=fnol,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        config=graph or GraphSettings(shared_mailbox=MAILBOX),
        fnol_config=fnol_config or FNOLSettings(),
    )
    return Harness(service, client, repository, session, ingestion, fnol)


def a_ledger_row(
    *,
    graph_message_id: str = "AAMk-1",
    internet_message_id: str | None = "<abc@broker.test>",
    status: MailIntakeStatus = MailIntakeStatus.PROCESSED,
    attempts: int = 1,
    marked_read: bool = True,
) -> MailIntakeMessage:
    record = MailIntakeMessage(
        mailbox=MAILBOX,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        received_at=datetime(2026, 8, 11, 9, 14, tzinfo=UTC),
        status=status.value,
        attempts=attempts,
        marked_read=marked_read,
    )
    record.id = uuid.uuid4()
    return record


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCollection:
    async def test_a_message_becomes_a_queued_notice(self) -> None:
        harness = build([a_message()])

        summary = await harness.service.poll()

        assert (summary.fetched, summary.ingested, summary.failed) == (1, 1, 0)
        assert summary.references == ["FNOL-2026-000001"]
        # Queued, not processed: extraction is the next phase's work.
        assert harness.fnol.queued == ["FNOL-2026-000001"]

        record = harness.records[0]
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.mailbox == MAILBOX
        assert record.internet_message_id == "<abc@broker.test>"
        assert record.sender_address == "priya@broker.test"
        assert record.fnol_case_id is not None
        assert record.processed_at is not None
        assert record.attempts == 1

    async def test_the_envelope_is_normalised_by_the_fnol_ingestion_layer(self) -> None:
        harness = build([a_message()])

        await harness.service.poll()

        # The notice was created by the shared mapping, under the machine actor.
        assert harness.ingestion.calls == [INTAKE_ACTOR]

    async def test_the_original_body_is_kept_on_the_ledger(self) -> None:
        harness = build([a_message()])

        await harness.service.poll()

        record = harness.records[0]
        assert record.body_content_type == "html"
        assert "<p>" in (record.body_content or "")

    async def test_an_attachment_is_stored_and_linked_to_its_document(self) -> None:
        harness = build([a_message(has_attachments=True)], {"AAMk-1": [an_attachment()]})

        summary = await harness.service.poll()

        assert summary.attachments_stored == 1
        assert harness.fnol.attached == ["survey-report.pdf"]

        attachment = harness.records[0].attachments[0]
        assert attachment.status == MailAttachmentStatus.STORED
        assert attachment.checksum_sha256 == "a" * 64
        assert attachment.storage_key is not None
        assert attachment.fnol_document_id is not None


class TestIdempotency:
    async def test_a_collected_message_is_not_collected_twice(self) -> None:
        harness = build([a_message()], seed=a_ledger_row())

        summary = await harness.service.poll()

        assert (summary.ingested, summary.duplicates) == (0, 1)
        assert harness.ingestion.calls == []
        assert len(harness.records) == 1

    async def test_a_redelivery_under_a_new_graph_id_is_caught_by_the_message_id(self) -> None:
        # The same notification, re-delivered: Outlook mints a new id, the
        # sending server's Message-ID does not change.
        harness = build(
            [a_message(message_id="AAMk-2")],
            seed=a_ledger_row(graph_message_id="AAMk-1"),
        )

        summary = await harness.service.poll()

        assert summary.duplicates == 1
        assert harness.ingestion.calls == []

    async def test_a_notice_that_already_exists_is_not_counted_as_new(self) -> None:
        existing = FNOLCase(
            reference="FNOL-2026-000099",
            channel=FNOLChannel.BROKER_EMAIL.value,
            received_at=datetime.now(UTC),
        )
        existing.id = uuid.uuid4()
        harness = build([a_message()], ingestion=FakeIngestion(existing=existing))

        summary = await harness.service.poll()

        assert (summary.ingested, summary.duplicates) == (0, 1)
        assert harness.fnol.queued == []
        assert harness.records[0].fnol_case_id == existing.id


class TestAttachments:
    async def test_an_oversized_attachment_is_refused_before_it_is_downloaded(self) -> None:
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": [an_attachment(name="site-video.pdf", size=60_000_000)]},
        )

        summary = await harness.service.poll()

        assert summary.attachments_skipped == 1
        assert harness.client.downloaded == []  # never transferred
        assert summary.ingested == 1

        attachment = harness.records[0].attachments[0]
        assert attachment.status == MailAttachmentStatus.SKIPPED
        assert "limit" in (attachment.detail or "")

    async def test_an_inline_image_is_skipped_by_default(self) -> None:
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": [an_attachment(name="signature.pdf", inline=True)]},
        )

        summary = await harness.service.poll()

        assert summary.attachments_skipped == 1
        assert "signature" in (harness.records[0].attachments[0].detail or "")

    async def test_an_inline_image_is_kept_where_the_mailbox_asks_for_it(self) -> None:
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": [an_attachment(name="damage.pdf", inline=True)]},
            graph=GraphSettings(shared_mailbox=MAILBOX, include_inline_attachments=True),
        )

        summary = await harness.service.poll()

        assert summary.attachments_stored == 1

    async def test_an_embedded_message_carries_no_file_and_is_skipped(self) -> None:
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": [an_attachment(odata_type="#microsoft.graph.itemAttachment")]},
        )

        summary = await harness.service.poll()

        assert summary.attachments_skipped == 1
        assert harness.client.downloaded == []

    async def test_a_file_type_claims_do_not_accept_does_not_lose_the_notice(self) -> None:
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": [an_attachment(name="macro.exe", size=32)]},
        )

        summary = await harness.service.poll()

        assert summary.ingested == 1  # the covering email is still a notification
        assert summary.attachments_skipped == 1
        attachment = harness.records[0].attachments[0]
        assert attachment.status == MailAttachmentStatus.SKIPPED
        assert "not a file type" in (attachment.detail or "")

    async def test_a_download_failure_is_recorded_and_the_notice_still_created(self) -> None:
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": [an_attachment()]},
            download_error=GraphError("The attachment could not be read."),
        )

        summary = await harness.service.poll()

        assert (summary.ingested, summary.attachments_failed) == (1, 1)
        assert harness.records[0].attachments[0].status == MailAttachmentStatus.FAILED

    async def test_only_as_many_documents_as_a_notice_may_carry_are_stored(self) -> None:
        attachments = [
            an_attachment(attachment_id=f"att-{index}", name=f"report-{index}.pdf")
            for index in range(3)
        ]
        harness = build(
            [a_message(has_attachments=True)],
            {"AAMk-1": attachments},
            fnol_config=FNOLSettings(max_documents_per_case=2),
        )

        summary = await harness.service.poll()

        assert (summary.attachments_stored, summary.attachments_skipped) == (2, 1)
        refused = next(
            row for row in harness.records[0].attachments if row.filename == "report-2.pdf"
        )
        assert "maximum" in (refused.detail or "")


class TestSweepCoverage:
    """The outage this module lost a live claim to, pinned from both ends.

    A message sat in the inbox for a day, flagged read by a human opening the
    shared mailbox, and every poll for twenty-two hours reported success on a
    folder it could no longer see it in. Two things had to be true at once: the
    sweep filtered on `isRead`, and it could not page past its own batch size.
    Each of the tests below fails if either comes back.
    """

    async def test_nothing_in_the_sequence_re_reads_the_read_flag(self) -> None:
        """The service half. The filter itself is the client's, and is pinned in
        `test_graph_client` and `test_config`; what this holds is that no step
        between listing and committing quietly reintroduces an `is_read` check."""
        opened = a_message()
        opened.is_read = True
        harness = build([opened])

        summary = await harness.service.poll()

        assert summary.ingested == 1
        assert harness.records[0].status == MailIntakeStatus.PROCESSED

    async def test_the_first_poll_of_an_empty_ledger_reads_the_whole_folder(self) -> None:
        harness = build([a_message()])

        await harness.service.poll()

        # `None`, not "now": a mailbox with history must be collectable, and a
        # watermark invented at first run would strand everything before it.
        assert harness.client.since_seen == [None]

    async def test_later_polls_sweep_back_from_the_newest_collected_message(self) -> None:
        harness = build(
            [a_message(message_id="AAMk-2", internet_message_id="<later@broker.test>")],
            seed=a_ledger_row(),
            graph=GraphSettings(shared_mailbox=MAILBOX, lookback_minutes=60),
        )

        await harness.service.poll()

        seeded_at = harness.repository.committed[0].received_at
        assert harness.client.since_seen == [seeded_at - timedelta(minutes=60)]

    async def test_the_lookback_keeps_a_late_delivery_inside_the_window(self) -> None:
        """A message that arrives out of order must not fall behind the watermark.

        Graph orders by `receivedDateTime`, and that is not the order things are
        delivered in. Without the overlap, a message stamped earlier than one
        already collected would sit below the floor and never be listed again.
        """
        collected_at = datetime(2026, 8, 11, 9, 14, tzinfo=UTC)
        late = a_message(message_id="AAMk-late", internet_message_id="<late@broker.test>")
        late.received_at = collected_at - timedelta(minutes=30)
        harness = build(
            [late],
            seed=a_ledger_row(),
            graph=GraphSettings(shared_mailbox=MAILBOX, lookback_minutes=1440),
        )

        summary = await harness.service.poll()

        assert summary.ingested == 1

    async def test_a_folder_holding_more_than_the_ledger_raises_the_alarm(self) -> None:
        harness = build([a_message()])
        harness.client.folder_total = 40

        summary = await harness.service.poll()

        # The count the mailbox reports, against the count intake believes. A
        # sweep that has gone blind logs `fetched=0` and looks healthy; this pair
        # is the only thing that contradicts it.
        assert (summary.folder_total, summary.ledger_total) == (40, 1)

    async def test_a_message_that_leaves_no_ledger_row_is_reported_as_dropped(self) -> None:
        """The exact check, and the only one that needs no assumptions.

        A message listed from the mailbox that ends the poll with no row is
        neither collected nor queued for retry — nothing will ever look at it
        again. This is the one failure that must never be inferred from a
        counter, because the counters come from the same code that dropped it.
        """
        harness = build([a_message()])

        async def _vanish(*_args: object, **_kwargs: object) -> None:
            return None

        # A collect that quietly does nothing, and a failure path that cannot
        # write either: the row never appears under any name.
        harness.service._collect = _vanish  # type: ignore[method-assign]

        summary = await harness.service.poll()

        assert summary.fetched == 1
        assert summary.dropped == 1

    async def test_unread_mail_that_the_sweep_returns_nothing_for_is_an_error(self) -> None:
        """The signature of the twenty-two hour outage, caught in one poll.

        Collection marks messages read, so unread mail the sweep fetched nothing
        for cannot be explained away. Unlike a folder-count comparison this
        cannot drift: it does not care how many rows the ledger has accumulated.
        """
        harness = build([])
        harness.client.folder_unread = 3

        summary = await harness.service.poll()

        assert summary.fetched == 0
        assert summary.sweep_blind is True

    async def test_a_quiet_mailbox_is_not_mistaken_for_a_blind_sweep(self) -> None:
        harness = build([])

        summary = await harness.service.poll()

        assert summary.sweep_blind is False

    async def test_a_reconciliation_that_cannot_be_read_does_not_fail_the_poll(self) -> None:
        harness = build([a_message()])
        harness.client.folder_stats_error = GraphError("Folder counts unavailable.")

        summary = await harness.service.poll()

        assert summary.ingested == 1
        assert summary.folder_total is None


class TestFailureHandling:
    async def test_one_failing_message_does_not_stop_the_batch(self) -> None:
        class FailsOnce(FakeIngestion):
            async def ingest(self, notification: object, *, actor: str):  # type: ignore[no-untyped-def]
                if not self.calls:
                    self.calls.append(actor)
                    raise RuntimeError("the reference sequence is locked")
                return await super().ingest(notification, actor=actor)

        harness = build(
            [
                a_message(message_id="AAMk-1"),
                a_message(message_id="AAMk-2", internet_message_id="<def@broker.test>"),
            ],
            ingestion=FailsOnce(),
        )

        summary = await harness.service.poll()

        assert (summary.failed, summary.ingested) == (1, 1)

        failed = next(row for row in harness.records if row.graph_message_id == "AAMk-1")
        assert failed.status == MailIntakeStatus.FAILED
        assert "the reference sequence is locked" in (failed.last_error or "")
        # One attempt, not two: the rollback discarded the first insert and the
        # failure path wrote the row again from scratch.
        assert failed.attempts == 1
        assert harness.session.rollbacks == 1

    async def test_a_failure_is_retried_on_the_next_poll(self) -> None:
        harness = build(
            [a_message()],
            seed=a_ledger_row(status=MailIntakeStatus.FAILED, attempts=1, marked_read=False),
        )

        summary = await harness.service.poll()

        assert summary.ingested == 1
        record = harness.records[0]
        assert record.status == MailIntakeStatus.PROCESSED
        assert record.attempts == 2

    async def test_a_message_that_keeps_failing_is_eventually_left_alone(self) -> None:
        harness = build(
            [a_message()],
            seed=a_ledger_row(status=MailIntakeStatus.FAILED, attempts=3),
            graph=GraphSettings(shared_mailbox=MAILBOX, max_attempts=3),
        )

        summary = await harness.service.poll()

        assert (summary.abandoned, summary.ingested, summary.failed) == (1, 0, 0)
        assert harness.ingestion.calls == []

    async def test_a_mailbox_that_cannot_be_listed_fails_the_poll(self) -> None:
        harness = build([])

        async def _explode(*_args: object, **_kwargs: object) -> list[GraphMessage]:
            raise GraphError("Microsoft Graph could not be reached.")

        harness.client.list_messages = _explode  # type: ignore[method-assign]

        with pytest.raises(GraphError):
            await harness.service.poll()


class TestMailboxState:
    async def test_the_message_is_marked_read_only_after_the_notice_is_durable(self) -> None:
        harness = build([a_message()])

        await harness.service.poll()

        assert harness.client.marked_read == ["AAMk-1"]
        assert harness.records[0].marked_read is True
        # Committed first, flagged second: a crash in between re-collects
        # rather than loses.
        assert harness.client.journal.index("commit") < harness.client.journal.index("mark_read")

    async def test_marking_read_can_be_turned_off(self) -> None:
        harness = build(
            [a_message()],
            graph=GraphSettings(shared_mailbox=MAILBOX, mark_as_read=False),
        )

        await harness.service.poll()

        assert harness.client.marked_read == []
        assert harness.records[0].marked_read is False

    async def test_nothing_is_moved_unless_a_folder_is_configured(self) -> None:
        harness = build([a_message()])

        await harness.service.poll()

        assert harness.client.moved == []

    async def test_a_configured_folder_is_used_and_recorded(self) -> None:
        harness = build(
            [a_message()],
            graph=GraphSettings(shared_mailbox=MAILBOX, move_to_folder="processed"),
        )

        await harness.service.poll()

        assert harness.client.moved == [("AAMk-1", "processed")]
        assert harness.records[0].moved_to_folder == "processed"

    async def test_a_flag_that_will_not_set_does_not_fail_the_message(self) -> None:
        harness = build([a_message()], mark_error=GraphError("Mailbox is read-only."))

        summary = await harness.service.poll()

        assert summary.ingested == 1
        assert harness.records[0].status == MailIntakeStatus.PROCESSED
        assert harness.records[0].marked_read is False

    async def test_a_flag_that_did_not_land_is_retried_on_the_next_poll(self) -> None:
        harness = build(
            [a_message()],
            seed=a_ledger_row(status=MailIntakeStatus.PROCESSED, marked_read=False),
        )

        summary = await harness.service.poll()

        assert summary.duplicates == 1
        assert harness.client.marked_read == ["AAMk-1"]


class TestTriggerEndpoint:
    """The manual trigger, from the outside."""

    async def test_an_unconfigured_deployment_says_so_rather_than_failing(
        self, app: FastAPI, auth_client: AsyncClient
    ) -> None:
        # The dependency is overridden rather than relying on empty settings: a
        # developer's own `.env` may carry real Graph credentials, and this test
        # has to stay meaningful either way.
        def _unconfigured() -> None:
            raise GraphNotConfiguredError()

        app.dependency_overrides[get_mail_client_dependency] = _unconfigured

        response = await auth_client.post("/api/v1/mail-intake/poll")

        # 503 and a sentence, not a 500: nothing is broken, nothing is set up.
        # Answered before a database session is opened, too.
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "graph_not_configured"

    async def test_a_poll_reports_what_it_collected(
        self, app: FastAPI, auth_client: AsyncClient
    ) -> None:
        harness = build([a_message()])
        app.dependency_overrides[build_mail_intake_context] = lambda: MailIntakeContext(
            session=harness.session,  # type: ignore[arg-type]
            messages=harness.repository,  # type: ignore[arg-type]
            intake=harness.service,
        )

        response = await auth_client.post("/api/v1/mail-intake/poll")

        assert response.status_code == 200
        body = response.json()
        assert body["ingested"] == 1
        assert body["references"] == ["FNOL-2026-000001"]
        assert body["mailbox"] == MAILBOX


class TestThePollLeavesAHealthRecord:
    """Every poll must leave a `mail_intake_runs` row, or nothing can see it stopped.

    This is the counterpart to the reconciliation tests above. Those check that a
    poll notices what *it* lost; these check that a poll which never happens is
    noticeable at all — which requires the polls that do happen to say so.
    """

    @pytest.mark.asyncio
    async def test_a_successful_poll_records_a_run(self) -> None:
        harness = build(messages=[a_message()])
        summary = await harness.service.poll()

        assert len(harness.repository.runs) == 1
        run = harness.repository.runs[0]
        assert run.ok
        assert run.mailbox == MAILBOX
        assert run.fetched == 1
        assert run.ingested == 1
        assert run.finished_at is not None
        assert summary.run_id == run.id

    @pytest.mark.asyncio
    async def test_a_poll_of_an_empty_mailbox_still_records_a_run(self) -> None:
        """The case the whole feature turns on.

        A quiet mailbox and a dead scheduler are the same from the outside unless
        the quiet polls leave evidence. If this row is missing, an idle weekend
        reads as an outage and the alarm gets muted.
        """
        harness = build(messages=[])
        await harness.service.poll()

        assert len(harness.repository.runs) == 1
        assert harness.repository.runs[0].ok
        assert harness.repository.runs[0].fetched == 0

    @pytest.mark.asyncio
    async def test_a_mailbox_that_cannot_be_listed_still_records_a_failed_run(self) -> None:
        """A poll that raises must not vanish — that is a poller alive and failing."""
        harness = build(messages=[a_message()])
        harness.client.list_messages = _raising(GraphError("Graph returned 401"))  # type: ignore[assignment]

        with pytest.raises(GraphError):
            await harness.service.poll()

        assert len(harness.repository.runs) == 1
        run = harness.repository.runs[0]
        assert not run.ok
        assert run.error is not None
        assert "401" in run.error

    @pytest.mark.asyncio
    async def test_the_trigger_is_recorded_so_a_human_poll_is_distinguishable(self) -> None:
        """The signature that has hidden this fault every previous time."""
        harness = build(messages=[a_message()])
        await harness.service.poll(trigger=MailIntakeTrigger.MANUAL)

        assert harness.repository.runs[0].trigger == MailIntakeTrigger.MANUAL

    @pytest.mark.asyncio
    async def test_a_scheduled_poll_is_the_default(self) -> None:
        harness = build(messages=[])
        await harness.service.poll()

        assert harness.repository.runs[0].trigger == MailIntakeTrigger.SCHEDULE

    @pytest.mark.asyncio
    async def test_the_run_carries_the_watermark_and_the_reconciliation_numbers(self) -> None:
        """So the row explains a verdict without anyone re-deriving it."""
        harness = build(messages=[a_message()])
        harness.client.folder_total = 9
        harness.client.folder_unread = 0
        await harness.service.poll()

        run = harness.repository.runs[0]
        assert run.swept_since is None  # empty ledger reads the whole folder
        assert run.folder_total == 9
        assert run.ledger_total == 1
        assert run.dropped == 0
        assert run.sweep_blind is False

    @pytest.mark.asyncio
    async def test_a_dropped_message_reaches_the_run_row(self) -> None:
        """The silent loss has to survive into the record the health check reads."""
        harness = build(messages=[a_message(message_id="AAMk-1")])
        harness.repository.count_recorded = _counting(0)  # type: ignore[assignment]
        await harness.service.poll()

        assert harness.repository.runs[0].dropped == 1

    @pytest.mark.asyncio
    async def test_a_failure_to_write_the_run_does_not_lose_the_batch(self) -> None:
        """Health bookkeeping must never cost a collected claim.

        The notice and its documents are committed message by message, well
        before this row is written. Losing the row degrades monitoring; taking the
        batch with it would be a worse bug than the one being monitored for.
        """
        harness = build(messages=[a_message()])

        def explode(run: MailIntakeRun) -> MailIntakeRun:
            raise RuntimeError("mail_intake_runs does not exist")

        harness.repository.add_run = explode  # type: ignore[assignment]
        summary = await harness.service.poll()

        assert summary.ingested == 1
        assert summary.run_id is None


def _raising(error: Exception):
    async def _fail(*_args: object, **_kwargs: object) -> list[GraphMessage]:
        raise error

    return _fail


def _counting(value: int):
    async def _count(**_kwargs: object) -> int:
        return value

    return _count
