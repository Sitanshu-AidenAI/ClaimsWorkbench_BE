"""`python -m app.services.mail` — collect the shared mailbox once, now.

For setting a mailbox up, and for a one-off catch-up. A deployment runs this as
a Celery beat schedule; this is how a person watches it work the first time,
without a broker or a worker in the way.
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, init_engine
from app.domain.enums import MailIntakeTrigger
from app.integrations.graph.client import close_mail_client
from app.services.mail.intake import MailIntakeSummary
from app.services.mail.runner import run_mail_intake

logger = get_logger(__name__)


async def _poll_once(limit: int | None) -> MailIntakeSummary:
    await init_engine(settings)
    try:
        return await run_mail_intake(limit=limit, trigger=MailIntakeTrigger.CLI)
    finally:
        await close_mail_client()
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect FNOL notifications from the mailbox.")
    parser.add_argument("--limit", type=int, default=None, help="Messages to collect this run.")
    args = parser.parse_args()

    configure_logging(settings)

    if not settings.graph.configured:
        # A missing credential is a setup mistake, and saying which four are
        # wanted is more use than a stack trace from the first Graph call.
        raise SystemExit(
            "Mailbox intake is not configured. Set CWB_GRAPH_TENANT_ID, CWB_GRAPH_CLIENT_ID, "
            "CWB_GRAPH_CLIENT_SECRET and CWB_GRAPH_SHARED_MAILBOX."
        )

    summary = asyncio.run(_poll_once(args.limit))
    logger.info(
        "mail_intake_cli_completed",
        mailbox=summary.mailbox,
        fetched=summary.fetched,
        ingested=summary.ingested,
        duplicates=summary.duplicates,
        failed=summary.failed,
        abandoned=summary.abandoned,
        references=summary.references,
    )


if __name__ == "__main__":
    main()
