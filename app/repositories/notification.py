"""Reads and writes for the notification panel.

Two queries carry this whole feature: "what should I see, newest first" and "how
many of those have I not read". Both are answered per-caller against a desk-wide
table, which is why `subject` threads through nearly every method here — a count
that ignored it would be the same number for everyone and the badge would never
clear.

The unread count is a `NOT EXISTS` against `notification_reads` rather than a
`LEFT JOIN ... WHERE read_at IS NULL`. Same answer, and it stays one index probe
per candidate row as the read table grows — which it does, once per person per
notification, faster than the notification table itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, Executable, delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationRead


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Writing --------------------------------------------------------------

    def add(self, notification: Notification) -> Notification:
        self._session.add(notification)
        return notification

    async def flush(self) -> None:
        """Push pending inserts so generated ids are available. Not a commit."""
        await self._session.flush()

    async def find_by_dedupe_key(self, dedupe_key: str) -> Notification | None:
        """The row this event has already produced, if it has.

        Checked before inserting so a re-delivered task is a query rather than an
        `IntegrityError` — which would poison the caller's transaction, and the
        caller's transaction is the one holding the claim notification.
        """
        statement = select(Notification).where(Notification.dedupe_key == dedupe_key)
        return (await self._session.execute(statement)).scalars().first()

    # -- Reading --------------------------------------------------------------

    def _unread_for(self, subject: str) -> ColumnElement[bool]:
        """`NOT EXISTS (this subject has read it)`, as a reusable predicate."""
        return ~(
            select(NotificationRead.id)
            .where(
                NotificationRead.notification_id == Notification.id,
                NotificationRead.subject == subject,
            )
            .exists()
        )

    async def list_for(
        self,
        subject: str,
        *,
        unread_only: bool = False,
        case_id: uuid.UUID | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[Sequence[Notification], int]:
        """A page of the panel, newest first, and the total behind it."""
        statement = select(Notification)
        counter = select(func.count(Notification.id))

        if unread_only:
            statement = statement.where(self._unread_for(subject))
            counter = counter.where(self._unread_for(subject))
        if case_id is not None:
            statement = statement.where(Notification.fnol_case_id == case_id)
            counter = counter.where(Notification.fnol_case_id == case_id)

        total = int((await self._session.execute(counter)).scalar_one())
        rows = (
            (
                await self._session.execute(
                    statement.order_by(Notification.occurred_at.desc(), Notification.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def unread_count(self, subject: str) -> int:
        """What the badge on the bell shows."""
        statement = select(func.count(Notification.id)).where(self._unread_for(subject))
        return int((await self._session.execute(statement)).scalar_one())

    async def read_subjects(
        self, notification_ids: Sequence[uuid.UUID], subject: str
    ) -> set[uuid.UUID]:
        """Which of these this person has read.

        One query for the whole page rather than reading `notification.reads` per
        row: that relationship is `selectin`-loaded and would carry every *other*
        person's read rows across the wire to answer a question about one.
        """
        if not notification_ids:
            return set()
        statement = select(NotificationRead.notification_id).where(
            NotificationRead.notification_id.in_(notification_ids),
            NotificationRead.subject == subject,
        )
        return set((await self._session.execute(statement)).scalars().all())

    async def get(self, notification_id: uuid.UUID) -> Notification | None:
        return await self._session.get(Notification, notification_id)

    # -- Marking read ---------------------------------------------------------

    async def _affected(self, statement: Executable) -> int:
        """Run a DML statement and report how many rows it touched.

        The cast is the honest way to say what `Session.execute` already returns
        for an INSERT or a DELETE: `rowcount` lives on `CursorResult`, and the
        declared return type is the wider `Result`, which does not carry it. Every
        caller below is DML, so the narrowing always holds.
        """
        result = await self._session.execute(statement)
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def mark_read(self, notification_ids: Sequence[uuid.UUID], subject: str) -> int:
        """Record that this person has seen these. Returns how many were new.

        `ON CONFLICT DO NOTHING` rather than a read-then-insert: marking read is
        the one write a client can fire twice by double-clicking, and the second
        one must be a no-op rather than a 500. Written through the Postgres
        dialect's insert because that is the only way to express it — and safely,
        because nothing else in this transaction depends on the ORM having seen
        these rows.
        """
        if not notification_ids:
            return 0

        statement = (
            pg_insert(NotificationRead)
            .values(
                [
                    {
                        "notification_id": notification_id,
                        "subject": subject,
                        "read_at": datetime.now(UTC),
                    }
                    for notification_id in dict.fromkeys(notification_ids)
                ]
            )
            .on_conflict_do_nothing(constraint="uq_notification_read")
        )
        return await self._affected(statement)

    async def mark_all_read(self, subject: str) -> int:
        """Clear the badge. Returns how many were newly marked.

        The candidate set is selected inside the same statement rather than fetched
        and re-sent, so a notification arriving between the two cannot be marked
        read without having been shown.
        """
        candidates = select(Notification.id).where(self._unread_for(subject)).subquery()
        statement = (
            pg_insert(NotificationRead)
            .from_select(
                ["notification_id", "subject", "read_at"],
                select(candidates.c.id, literal(subject), func.now()),
            )
            .on_conflict_do_nothing(constraint="uq_notification_read")
        )
        return await self._affected(statement)

    # -- Deletion --------------------------------------------------------------

    async def count_for_case(self, case_id: uuid.UUID) -> int:
        """How many notifications a notice carries. Read before deleting it."""
        statement = select(func.count(Notification.id)).where(Notification.fnol_case_id == case_id)
        return int((await self._session.execute(statement)).scalar_one())

    async def delete_for_case(self, case_id: uuid.UUID) -> int:
        """Remove a notice's notifications. Its read rows go by cascade.

        Called from the deletion path for the same reason it removes the mailbox
        ledger: a panel row saying "FNOL-2026-000123 processed successfully" that
        opens onto nothing is a lie about what happened, and there is no case left
        for it to be true about.
        """
        return await self._affected(
            delete(Notification).where(Notification.fnol_case_id == case_id)
        )


__all__ = ["NotificationRepository"]
