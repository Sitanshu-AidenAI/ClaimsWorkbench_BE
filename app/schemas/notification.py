"""Request and response shapes for the notification panel.

Mapped explicitly, like every other schema module here. `context` is the one
loose field and it is published as-is deliberately: the panel renders whatever
metadata a kind chose to carry, and a whitelist here would mean a schema change
every time a new kind wants to say something. Nothing secret goes in it — the
producers in `app.services.notifications.service` are the guard, and they put an
envelope and a count in there, never a body or a credential.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.notification import Notification
from app.schemas.common import SchemaBase


class NotificationSummary(SchemaBase):
    id: uuid.UUID
    kind: str
    tone: str
    title: str
    body: str
    occurred_at: datetime
    fnol_case_id: uuid.UUID | None
    fnol_reference: str | None
    #: Whether *this caller* has read it. Computed per request — the row itself is
    #: desk-wide, so there is no `read` column to project.
    read: bool
    context: dict[str, object]


class NotificationListResult(SchemaBase):
    items: list[NotificationSummary]
    total: int
    #: What the badge on the bell shows. Ledger-wide for this caller, so it is not
    #: bounded by the page — a panel showing 25 of 90 still says 90 unread.
    unread: int
    page: int
    page_size: int


class MarkReadRequest(SchemaBase):
    """Which notifications the caller has now seen.

    A list rather than one id per call: the panel marks everything it just
    displayed, and thirty requests to acknowledge one dropdown opening is thirty
    round trips and thirty audit-worthy writes.
    """

    notification_ids: list[uuid.UUID]


class MarkReadResult(SchemaBase):
    #: How many rows were newly marked. Zero is a normal answer — re-acknowledging
    #: what was already read is a no-op, not an error.
    marked: int
    unread: int


def to_summary(row: Notification, *, read: bool) -> NotificationSummary:
    return NotificationSummary(
        id=row.id,
        kind=row.kind,
        tone=row.tone,
        title=row.title,
        body=row.body,
        occurred_at=row.occurred_at,
        fnol_case_id=row.fnol_case_id,
        fnol_reference=row.fnol_reference,
        read=read,
        context=dict(row.context or {}),
    )
