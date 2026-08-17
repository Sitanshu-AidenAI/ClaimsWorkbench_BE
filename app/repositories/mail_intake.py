"""Reads and writes for the mailbox ledger.

Every lookup here exists to answer one question: has this message been dealt
with? It is asked twice per message — once on the Graph id, once on the
`Message-ID` the sending server wrote — because those are the two ways the same
notification comes round again.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MailIntakeStatus
from app.models.mail_intake import MailIntakeAttachment, MailIntakeMessage


class MailIntakeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Messages ------------------------------------------------------------

    def add(self, message: MailIntakeMessage) -> MailIntakeMessage:
        self._session.add(message)
        return message

    async def flush(self) -> None:
        """Push pending inserts so generated ids are available. Not a commit."""
        await self._session.flush()

    async def get(self, message_id: uuid.UUID) -> MailIntakeMessage | None:
        return await self._session.get(MailIntakeMessage, message_id)

    async def find_by_graph_id(self, graph_message_id: str) -> MailIntakeMessage | None:
        statement = select(MailIntakeMessage).where(
            MailIntakeMessage.graph_message_id == graph_message_id
        )
        return (await self._session.execute(statement)).scalars().first()

    async def find_by_internet_message_id(
        self, internet_message_id: str
    ) -> MailIntakeMessage | None:
        statement = select(MailIntakeMessage).where(
            MailIntakeMessage.internet_message_id == internet_message_id
        )
        return (await self._session.execute(statement)).scalars().first()

    async def find_existing(
        self, *, graph_message_id: str, internet_message_id: str | None
    ) -> MailIntakeMessage | None:
        """The ledger row this message has already produced, under either id."""
        existing = await self.find_by_graph_id(graph_message_id)
        if existing is not None:
            return existing
        if internet_message_id:
            return await self.find_by_internet_message_id(internet_message_id)
        return None

    async def list_messages(
        self,
        *,
        statuses: Sequence[str] | None = None,
        mailbox: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[MailIntakeMessage], int]:
        """A page of the ledger, newest first, and the total behind it."""
        statement = select(MailIntakeMessage)
        counter = select(func.count(MailIntakeMessage.id))

        if statuses:
            statement = statement.where(MailIntakeMessage.status.in_(statuses))
            counter = counter.where(MailIntakeMessage.status.in_(statuses))
        if mailbox:
            statement = statement.where(MailIntakeMessage.mailbox == mailbox)
            counter = counter.where(MailIntakeMessage.mailbox == mailbox)

        total = int((await self._session.execute(counter)).scalar_one())
        rows = (
            (
                await self._session.execute(
                    statement.order_by(MailIntakeMessage.received_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def status_counts(self) -> dict[str, int]:
        statement = select(MailIntakeMessage.status, func.count(MailIntakeMessage.id)).group_by(
            MailIntakeMessage.status
        )
        return {status: int(count) for status, count in (await self._session.execute(statement))}

    async def count_by_status(self, status: MailIntakeStatus) -> int:
        statement = select(func.count(MailIntakeMessage.id)).where(
            MailIntakeMessage.status == status.value
        )
        return int((await self._session.execute(statement)).scalar_one())

    # -- Deletion --------------------------------------------------------------

    async def list_for_case(self, case_id: uuid.UUID) -> Sequence[MailIntakeMessage]:
        """Every collected message that produced, or was filed against, one notice."""
        statement = (
            select(MailIntakeMessage)
            .where(MailIntakeMessage.fnol_case_id == case_id)
            .order_by(MailIntakeMessage.received_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def delete_message(self, message: MailIntakeMessage) -> None:
        """Remove a collected message and its attachment ledger.

        The attachment rows go with it by cascade. Their bytes do not live here —
        `storage_key` on an attachment names the same blob the `FNOLDocument`
        names, so whoever removes the document removes the object, and doing it
        twice from here would delete a file a surviving document still points at.
        """
        await self._session.delete(message)

    # -- Attachments ---------------------------------------------------------

    def add_attachment(self, attachment: MailIntakeAttachment) -> MailIntakeAttachment:
        self._session.add(attachment)
        return attachment

    async def list_attachments(self, message_id: uuid.UUID) -> Sequence[MailIntakeAttachment]:
        statement = (
            select(MailIntakeAttachment)
            .where(MailIntakeAttachment.mail_intake_message_id == message_id)
            .order_by(MailIntakeAttachment.filename)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def find_attachment(
        self, message_id: uuid.UUID, graph_attachment_id: str
    ) -> MailIntakeAttachment | None:
        statement = select(MailIntakeAttachment).where(
            MailIntakeAttachment.mail_intake_message_id == message_id,
            MailIntakeAttachment.graph_attachment_id == graph_attachment_id,
        )
        return (await self._session.execute(statement)).scalars().first()
