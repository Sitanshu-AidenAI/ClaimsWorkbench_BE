"""Mailbox intake endpoints.

Two routes, and both are for the people who run the service rather than for the
claims desk: trigger a poll, and read what the last polls did. Collection itself
is meant to be a scheduled worker — this router exists so that a poll can be run
on demand while a mailbox is being set up, and so that "why has nothing arrived
since Tuesday" has an answer that does not require database access.

The trigger is gated on the intake write roles, because a poll creates
notifications. Note what it is *not* gated on: the Graph credentials are the
service's own, so nothing here needs the caller to have any relationship with
Microsoft — Keycloak authorises the human, Graph authorises the service.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps.auth import require_roles
from app.api.deps.services import MailIntakeContextDep
from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import FNOL_READ_ROLES, FNOL_WRITE_ROLES, MailIntakeStatus
from app.schemas import mail_intake as api

logger = get_logger(__name__)

router = APIRouter(prefix="/mail-intake", tags=["mail-intake"])

ReadAccess = Annotated[Principal, Depends(require_roles(*FNOL_READ_ROLES))]
WriteAccess = Annotated[Principal, Depends(require_roles(*FNOL_WRITE_ROLES))]


@router.post(
    "/poll",
    response_model=api.MailIntakeRunResult,
    summary="Collect waiting messages from the shared mailbox",
)
async def poll_mailbox(
    # The principal is declared first so authorisation is settled before the
    # Graph client and the session are built. A caller who may not do this
    # should be refused, not told what the mailbox configuration looks like.
    principal: WriteAccess,
    context: MailIntakeContextDep,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> api.MailIntakeRunResult:
    """Run one intake batch now.

    The same call the scheduled worker makes. Safe to repeat: a message already
    collected is recognised on its Graph id or its `Message-ID` and produces no
    second notification.
    """
    logger.info("mail_intake_triggered", actor=principal.username or principal.subject)
    summary = await context.intake.poll(limit=limit)
    return api.to_run_result(summary)


@router.get(
    "/messages",
    response_model=api.MailIntakeListResult,
    summary="What the mailbox has delivered, and what became of it",
)
async def list_messages(
    principal: ReadAccess,
    context: MailIntakeContextDep,
    status_filter: Annotated[list[MailIntakeStatus] | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> api.MailIntakeListResult:
    del principal
    rows, total = await context.messages.list_messages(
        statuses=[value.value for value in status_filter] if status_filter else None,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return api.MailIntakeListResult(
        items=[api.to_message_summary(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        status_counts=await context.messages.status_counts(),
    )
