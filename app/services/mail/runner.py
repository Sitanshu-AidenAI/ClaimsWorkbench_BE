"""Assembling mail intake, and running it outside a request.

The wiring lives here rather than in the API's dependency graph because the
caller that matters most is a Celery beat schedule, which has no request to hang
a dependency off. The route uses the same builder, so a manual trigger and a
scheduled poll run exactly the same code — the difference is only who asked.

Note what is *not* wired in: the FNOL pipeline. Intake stops at a stored,
queued notice. Extraction is the next phase and will consume what this leaves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.enums import MailIntakeTrigger
from app.integrations.graph.client import GraphMailClient, get_mail_client
from app.repositories.audit import AuditRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.mail_intake import MailIntakeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.reference import ReferenceRepository
from app.services.documents.service import DocumentProcessingService
from app.services.fnol.audit import AuditService
from app.services.fnol.ingestion import FNOLIngestionService
from app.services.fnol.service import FNOLService
from app.services.mail.intake import MailIntakeService, MailIntakeSummary
from app.services.notifications.service import NotificationService

logger = get_logger(__name__)


def build_mail_intake_service(
    session: AsyncSession,
    *,
    client: GraphMailClient | None = None,
    config: Settings | None = None,
) -> MailIntakeService:
    """Build the service and its collaborators against one session."""
    config = config or settings
    cases = FNOLRepository(session)
    audit = AuditService(AuditRepository(session))

    return MailIntakeService(
        session=session,
        messages=MailIntakeRepository(session),
        ingestion=FNOLIngestionService(cases, ReferenceRepository(session), audit),
        fnol=FNOLService(cases, audit, documents=DocumentProcessingService()),
        client=client or get_mail_client(config.graph),
        notifications=NotificationService(NotificationRepository(session)),
        config=config.graph,
        fnol_config=config.fnol,
    )


async def run_mail_intake(
    *,
    limit: int | None = None,
    trigger: str = MailIntakeTrigger.SCHEDULE,
    client: GraphMailClient | None = None,
    config: Settings | None = None,
) -> MailIntakeSummary:
    """Open a session, poll the mailbox, and hand back what happened.

    The session is opened directly rather than through `session_scope`: the
    intake service commits once per message, and a scope that committed again at
    the end would blur the boundary that makes a partial batch safe.
    """
    factory = get_session_factory()
    async with factory() as session:
        service = build_mail_intake_service(session, client=client, config=config)
        try:
            return await service.poll(limit=limit, trigger=trigger)
        except Exception:
            await session.rollback()
            raise


async def prune_mail_intake_run_records(config: Settings | None = None) -> int:
    """Drop run records past their retention. Returns how many went.

    Here rather than on the health service because it is housekeeping on a table,
    needs no Graph client, and its only caller is a Celery task.
    """
    config = config or settings
    cutoff = datetime.now(UTC) - timedelta(days=config.graph.run_retention_days)
    factory = get_session_factory()
    async with factory() as session:
        removed = await MailIntakeRepository(session).prune_runs(before=cutoff)
        await session.commit()
        return removed
