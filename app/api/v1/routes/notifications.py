"""The notification panel's endpoints.

Three routes: read the panel, acknowledge what was shown, acknowledge everything.

Gated on `FNOL_READ_ROLES` rather than on the write roles. A notification is an
observation about the desk — an email arrived, a notice was read — and every role
that may look at an intake queue may be told that its contents changed. Nothing
here is a decision, which is why marking read is a `POST` on the reader's own
state rather than a write on the notice.

**Everything is scoped to the caller's token subject.** The notifications
themselves are desk-wide, because a broker's email arrives at the claims desk and
not at a person; the *read* state is per-person, so one officer clearing their
badge does not clear it for the rest of the desk. `principal.subject` is the key
throughout, taken from the verified token and never from the request body — a
client that could name whose panel to mark read could clear someone else's.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps.auth import require_roles
from app.api.deps.services import NotificationContextDep
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import FNOL_READ_ROLES
from app.schemas import notification as api

logger = get_logger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

ReadAccess = Annotated[Principal, Depends(require_roles(*FNOL_READ_ROLES))]


@router.get(
    "",
    response_model=api.NotificationListResult,
    summary="What the desk has been told, newest first",
)
async def list_notifications(
    principal: ReadAccess,
    context: NotificationContextDep,
    unread_only: Annotated[bool, Query()] = False,
    case_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> api.NotificationListResult:
    """The panel behind the bell.

    Polled, so it is deliberately cheap: two counts and one indexed page. The
    unread total is computed ledger-wide rather than over the returned page — a
    panel showing the newest 25 of 90 unread must still say 90 on the badge.
    """
    rows, total = await context.notifications.list_for(
        principal.subject,
        unread_only=unread_only,
        case_id=case_id,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    # One query for the whole page rather than touching `row.reads` per row, which
    # would load every other person's read rows to answer a question about one.
    read_ids = await context.notifications.read_subjects(
        [row.id for row in rows], principal.subject
    )

    return api.NotificationListResult(
        items=[api.to_summary(row, read=row.id in read_ids) for row in rows],
        total=total,
        unread=await context.notifications.unread_count(principal.subject),
        page=page,
        page_size=page_size,
    )


@router.post(
    "/read",
    response_model=api.MarkReadResult,
    summary="Acknowledge the notifications that have been shown",
)
async def mark_read(
    principal: ReadAccess,
    context: NotificationContextDep,
    body: api.MarkReadRequest,
) -> api.MarkReadResult:
    """Mark these as read for the calling user.

    Safe to repeat — the second call marks nothing and answers `marked: 0` rather
    than failing, which is what lets the panel fire this on every open without
    tracking what it has already sent.
    """
    marked = await context.notifications.mark_read(body.notification_ids, principal.subject)
    await context.session.commit()
    return api.MarkReadResult(
        marked=marked,
        unread=await context.notifications.unread_count(principal.subject),
    )


@router.post(
    "/read-all",
    response_model=api.MarkReadResult,
    summary="Clear the badge",
)
async def mark_all_read(
    principal: ReadAccess,
    context: NotificationContextDep,
) -> api.MarkReadResult:
    marked = await context.notifications.mark_all_read(principal.subject)
    await context.session.commit()
    logger.info(
        "notifications_all_read",
        actor=principal.username or principal.subject,
        marked=marked,
    )
    return api.MarkReadResult(
        marked=marked,
        unread=await context.notifications.unread_count(principal.subject),
    )


@router.get(
    "/{notification_id}",
    response_model=api.NotificationSummary,
    summary="One notification",
)
async def get_notification(
    principal: ReadAccess,
    context: NotificationContextDep,
    notification_id: uuid.UUID,
) -> api.NotificationSummary:
    row = await context.notifications.get(notification_id)
    if row is None:
        raise NotFoundError(f"No notification exists with id {notification_id}.")

    read_ids = await context.notifications.read_subjects([row.id], principal.subject)
    return api.to_summary(row, read=row.id in read_ids)
