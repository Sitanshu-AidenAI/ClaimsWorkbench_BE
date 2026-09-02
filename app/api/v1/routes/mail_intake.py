"""Mailbox intake endpoints.

Routes for the people who run the service rather than for the claims desk:
trigger a poll, read what the last polls did, and **receive Graph change
notifications**. Collection itself is meant to be a scheduled worker or a
notification — the manual trigger exists so that a poll can be run while a mailbox
is being set up, and so that "why has nothing arrived since Tuesday" has an answer
that does not require database access.

The trigger is gated on the intake write roles, because a poll creates
notifications. Note what it is *not* gated on: the Graph credentials are the
service's own, so nothing here needs the caller to have any relationship with
Microsoft — Keycloak authorises the human, Graph authorises the service.

**The two notification routes are deliberately unauthenticated**, and that is the
one thing in this module to read carefully. Graph has no bearer token to present,
so no `Depends(require_roles(...))` can guard them; a shared secret does, checked
against every request, plus the subscription id — see
`app.domain.mail_subscription.authentic`. They are public endpoints and are
written as such: fail closed, log nothing sensitive, do no work inline.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.api.deps.auth import require_roles
from app.api.deps.services import MailIntakeContextDep, MailIntakeHealthContextDep
from app.core.logging import get_logger
from app.core.security import Principal
from app.db.session import get_session_factory
from app.domain.enums import (
    FNOL_READ_ROLES,
    FNOL_WRITE_ROLES,
    MailIntakeStatus,
    MailIntakeTrigger,
)
from app.integrations.graph.client import get_mail_client
from app.repositories.mail_intake import MailIntakeRepository
from app.schemas import mail_intake as api
from app.services.mail.subscription import MailSubscriptionService
from app.workers.tasks import (
    ingest_notified_message,
    poll_mail_intake,
    renew_mail_subscription,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/mail-intake", tags=["mail-intake"])

#: A change-notification batch is a handful of small objects. Anything larger is
#: not Graph, and reading it into memory is the cheapest denial of service there
#: is on a public route.
_MAX_NOTIFICATION_BYTES = 256 * 1024

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
    summary = await context.intake.poll(limit=limit, trigger=MailIntakeTrigger.MANUAL)
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


@router.get(
    "/status",
    response_model=api.MailIntakeStatusResult,
    summary="Is mail actually being collected?",
)
async def intake_status(
    principal: ReadAccess,
    context: MailIntakeHealthContextDep,
    runs: Annotated[int, Query(ge=1, le=50)] = 10,
) -> api.MailIntakeStatusResult:
    """Report whether the poller is alive, from a process that is not the poller.

    This is the endpoint that closes the hole every other check in mail intake
    shares. `_reconcile`, `sweep_blind` and the truncation warning are all
    computed *during* a poll, so all three go quiet in the one case that has
    actually bitten this deployment: no poll running at all. A dead scheduler and
    an empty mailbox leave identical evidence — until something outside the
    scheduler is asked.

    Note the dependency it does *not* take: no Graph client. Building one raises
    when credentials are missing, so a health route that needed a mailbox could
    not report a missing mailbox, and a tenant outage would take down the very
    endpoint whose job is to name the tenant outage. The verdict comes out of
    `mail_intake_runs`, which is a local table.

    Read access rather than write: this answers a question, and the people who
    ask it first are the handlers wondering where a broker's email went.
    """
    del principal
    report = await context.health.report()
    recent = await context.messages.list_runs(limit=runs)

    if not report.healthy:
        # Logged here as well as returned, because the caller is often a UI panel
        # whose reader is not watching a terminal — and because this line in the
        # API's log is the one trace of the fault that exists when the worker is
        # not running to write any.
        logger.error(
            "mail_intake_unhealthy",
            state=report.state.value,
            mailbox=report.mailbox,
            sweep_collecting=report.sweep_collecting,
            webhook_collecting=report.webhook_collecting,
            last_run_at=report.last_run_at.isoformat() if report.last_run_at else None,
            last_run_age_seconds=report.last_run_age_seconds,
            config_stale_seconds=report.config_stale_seconds,
            detail=report.detail,
        )

    return api.to_status_result(report, list(recent))


# ---------------------------------------------------------------------------
# Change notifications
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _subscription_service() -> AsyncIterator[MailSubscriptionService]:
    """A subscription service on its own short-lived session.

    Opened here rather than injected, because FastAPI resolves a route's
    dependencies *before* the function body runs — so an injected context would
    open a database session and build a Graph client for a validation handshake
    that needs neither, and a subscription would be impossible to create while
    Postgres was down. The same arrangement the Celery tasks use.

    Read-only: `authentic` only reads, so there is nothing to commit.
    """
    factory = get_session_factory()
    async with factory() as session:
        yield MailSubscriptionService(MailIntakeRepository(session), get_mail_client())


@router.post(
    "/notifications",
    summary="Receive Graph change notifications (public)",
    include_in_schema=False,
)
async def receive_notifications(
    request: Request,
    #: Graph's own casing, not ours. It arrives as `validationToken` on the query
    #: string and FastAPI binds by parameter name, so renaming it would silently
    #: stop matching and every subscription create would fail its handshake.
    validationToken: Annotated[str | None, Query()] = None,
) -> Response:
    """Graph telling us a message arrived.

    Three things happen here and the order matters.

    **The validation handshake comes first.** On every create *and* every renewal,
    Graph calls this URL with `?validationToken=...` and expects that exact string
    back as `text/plain`, HTTP 200, within ten seconds. It arrives before any
    subscription exists, so it cannot be authenticated and must not be — a
    handshake that required a secret would make the subscription impossible to
    create. Echoing an opaque token back to the caller that sent it discloses
    nothing.

    **Then authenticity.** `clientState` proves the caller knows a secret only
    Graph was told; the subscription id proves the notification belongs to a
    subscription we made. Failing either is a flat 202 with nothing collected —
    **not** a 401. A public endpoint that distinguishes "wrong secret" from
    "unknown subscription" is an oracle for guessing both.

    **Then the work, elsewhere.** Graph's deadline per batch is about three seconds
    and it retries on a timeout, eventually dropping the subscription — so a
    handler that fetched a message and ran extraction inline would lose the
    subscription the first time a document was slow. Each id goes to Celery and
    this returns 202 at once.
    """
    if validationToken is not None:
        #: Not logged with the token in it. It is single-use and harmless, and a
        #: habit of logging whatever arrives on a public route is how the next
        #: secret ends up in a log file.
        logger.info("mail_notification_validated")
        return PlainTextResponse(validationToken, status_code=status.HTTP_200_OK)

    payload = await _notification_body(request)
    items = [item for item in (payload.get("value") or []) if isinstance(item, dict)]
    if not items:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    accepted = 0
    async with _subscription_service() as subscription:
        for item in items:
            if not await subscription.authentic(
                client_state=item.get("clientState"),
                subscription_id=item.get("subscriptionId"),
            ):
                logger.warning(
                    "mail_notification_rejected",
                    subscription_id=item.get("subscriptionId"),
                )
                continue

            message_id = str((item.get("resourceData") or {}).get("id") or "")
            if not message_id:
                continue

            ingest_notified_message.delay(message_id)
            accepted += 1

    logger.info("mail_notification_received", received=len(items), accepted=accepted)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/notifications/lifecycle",
    summary="Receive Graph subscription lifecycle events (public)",
    include_in_schema=False,
)
async def receive_lifecycle(
    request: Request,
    validationToken: Annotated[str | None, Query()] = None,
) -> Response:
    """Graph telling us about the subscription itself rather than about a message.

    Three events, and each has a different right answer:

    * **`reauthorizationRequired`** — renew now. Graph sends this when the
      subscription needs re-authorising before its expiry, which is early enough
      to act on and easy to ignore into a lapse.
    * **`subscriptionRemoved`** — Graph has dropped it. Recreate.
    * **`missed`** — Graph could not deliver some notifications and is saying so.
      **This is the moment the poll earns its place**: Graph does not say *what* it
      missed, so the only way to find out is to sweep the mailbox.

    The first two are both `ensure`, which already decides between renewing and
    recreating, so all three are handed to a task rather than branched on here.
    """
    if validationToken is not None:
        logger.info("mail_lifecycle_validated")
        return PlainTextResponse(validationToken, status_code=status.HTTP_200_OK)

    payload = await _notification_body(request)
    items = [item for item in (payload.get("value") or []) if isinstance(item, dict)]
    if not items:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    async with _subscription_service() as subscription:
        for item in items:
            if not await subscription.authentic(
                client_state=item.get("clientState"),
                subscription_id=item.get("subscriptionId"),
            ):
                logger.warning(
                    "mail_lifecycle_rejected", subscription_id=item.get("subscriptionId")
                )
                continue

            event = str(item.get("lifecycleEvent") or "")
            logger.info("mail_lifecycle_event", event=event)

            if event == "missed":
                poll_mail_intake.delay()
            else:
                renew_mail_subscription.delay()

    return Response(status_code=status.HTTP_202_ACCEPTED)


async def _notification_body(request: Request) -> dict[str, Any]:
    """The JSON body, size-capped, and never raising on a bad one.

    A public endpoint has to survive whatever is posted to it. A malformed body
    becomes an empty payload rather than a 422: answering anything but 2xx teaches
    Graph to retry, and a 422 to a hostile caller confirms the route is real.
    """
    raw = await request.body()
    if len(raw) > _MAX_NOTIFICATION_BYTES:
        logger.warning("mail_notification_oversized", bytes=len(raw))
        return {}
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        logger.warning("mail_notification_unparseable")
        return {}
    return parsed if isinstance(parsed, dict) else {}
