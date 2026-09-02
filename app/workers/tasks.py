"""Celery tasks.

Celery workers are synchronous, but the application's data access is async.
`run_async` bridges the two with a per-process event loop, so tasks can reuse
the same repositories and services the API uses instead of duplicating them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.core.config import env_file_changed_since_load, settings
from app.core.logging import get_logger
from app.db.pool import close_pool, init_pool
from app.db.session import dispose_engine, init_engine
from app.domain.enums import MailIntakeTrigger
from app.integrations.graph.client import close_mail_client
from app.services.cache import close_redis, init_redis
from app.services.intelligence.indexing import TransientIndexError
from app.services.intelligence.runner import (
    claim_queued_cases,
    release_stale_indexing,
    release_stale_processing,
    run_case_index,
    run_case_pipeline,
    run_document_index,
)
from app.services.mail.runner import (
    prune_mail_intake_run_records,
    run_mail_intake,
    run_notified_message,
    run_subscription_renewal,
)
from app.services.policies.ingestion import TransientIngestError
from app.services.policies.runner import (
    release_stale_ingest,
    run_document_ingest,
    run_pending_ingest,
)
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """One event loop per worker process, reused across tasks.

    Reusing the loop is what lets the asyncpg pool and Redis client survive
    between tasks rather than being rebuilt on every invocation.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop.run_until_complete(_startup())
    return _loop


async def _startup() -> None:
    await init_engine(settings)
    await init_pool(settings)
    await init_redis(settings)


async def _shutdown() -> None:
    await close_redis()
    await close_pool()
    await close_mail_client()
    await dispose_engine()


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine on the worker's event loop."""
    return _get_loop().run_until_complete(coro)


def shutdown_worker_resources() -> None:
    """Tear down the worker's async resources. Wired to worker shutdown."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.run_until_complete(_shutdown())
        _loop.close()
        _loop = None


def _warn_if_configuration_is_stale() -> None:
    """Say so, loudly and every tick, when this process is running superseded config.

    A worker fixes its settings at import and can never pick up a later `.env`
    edit. The failure that causes is not a crash — it is a process that keeps
    succeeding against configuration nobody believes it still has. Mailbox intake
    spent twenty-two hours that way: the flag it needed had been changed thirteen
    minutes after it started, and every one of the 5,272 polls that followed
    reported a clean run over an empty result.

    Repeated rather than logged once at startup on purpose. The edit usually
    happens *while* the worker is running, so a startup line would already be
    scrolled away by the time anyone came looking, and the operator's question is
    always asked in the present tense: is what I changed live yet?
    """
    stale_by = env_file_changed_since_load()
    if stale_by is None:
        return
    logger.warning(
        "worker_configuration_stale",
        env_file_edited_seconds_ago=round(stale_by, 1),
        detail=(
            "`.env` has been edited since this worker loaded its settings. Nothing it "
            "does reflects that edit, and nothing will until the worker AND beat are "
            "restarted. This process is running the old configuration."
        ),
    )


@celery_app.task(name="app.workers.tasks.heartbeat")
def heartbeat() -> dict[str, str]:
    """Beat-scheduled no-op, so a broken schedule is visible in logs."""
    logger.info("celery_heartbeat")
    return {"status": "ok"}


@celery_app.task(
    name="app.workers.tasks.ping_dependencies",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def ping_dependencies(self: Any) -> dict[str, bool]:
    """Verify the worker can reach Postgres and Redis. Also a template for
    async-capable tasks: do the work in a coroutine, bridge with `run_async`."""

    async def _check() -> dict[str, bool]:
        from app.db import pool
        from app.services.cache import ping_redis

        return {
            "postgres": await pool.fetchval("SELECT 1") == 1,
            "redis": await ping_redis(),
        }

    try:
        return run_async(_check())
    except Exception as exc:
        logger.error("celery_ping_failed", error=str(exc))
        raise self.retry(exc=exc) from exc


@celery_app.task(
    name="app.workers.tasks.ingest_notified_message",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
    retry_jitter=True,
)
def ingest_notified_message(graph_message_id: str) -> dict[str, int | str]:
    """Collect one message, named by a Graph change notification.

    **Retried, unlike the poll.** The poll's own docstring explains why it is not:
    the next scheduled sweep is its retry. A notification has no next sweep — Graph
    said this message exists and said it once — so a transient failure here is a
    message that never arrives. The ledger's attempt counter still bounds it, and
    the unique keys still make a retry that succeeds after a partial write safe.

    Kept small on purpose. The endpoint that queues this has about three seconds
    before Graph gives up on it, so the fetch, the attachments and the pipeline all
    happen here rather than there.
    """
    if not settings.graph.configured:
        logger.warning("mail_notification_skipped_unconfigured")
        return {"status": "not_configured"}

    summary = run_async(run_notified_message(graph_message_id))

    #: Hand it straight on, the same as the poll does. A notice that has just
    #: landed should not wait out a `process_queued_cases` interval.
    if summary.ingested:
        process_queued_cases.delay()

    return {
        "status": "ok",
        "graph_message_id": graph_message_id,
        "ingested": summary.ingested,
        "duplicates": summary.duplicates,
        "failed": summary.failed,
        "dropped": summary.dropped,
    }


@celery_app.task(name="app.workers.tasks.renew_mail_subscription")
def renew_mail_subscription() -> dict[str, str | None]:
    """Create the Graph subscription, or extend the one we hold.

    **The task that stops intake going quiet.** Graph caps a mail subscription at
    4230 minutes — 70½ hours — so a subscription created once and left alone dies
    inside three days, and the failure is silent: the mailbox simply stops
    notifying and the sweep goes on reporting healthy polls of nothing new.

    Safe to call from anywhere and often. `MailSubscriptionService.ensure` decides
    between leaving it alone, renewing it and recreating it, so beat's tick, a
    `reauthorizationRequired` lifecycle event and a manual nudge are all the same
    call.
    """
    if not settings.graph.webhook_ready:
        logger.debug("mail_subscription_skipped_unconfigured")
        return {"status": "not_configured", "subscription_id": None}

    try:
        subscription_id = run_async(run_subscription_renewal())
    except Exception as exc:
        #: Logged and swallowed rather than raised. A failed renewal is recoverable
        #: — the next tick tries again, and there are four before the margin runs
        #: out — and a task that raised would retry against a Graph that is very
        #: likely still down.
        logger.error("mail_subscription_renewal_failed", error=str(exc), exc_info=exc)
        return {"status": "failed", "subscription_id": None}

    return {"status": "ok", "subscription_id": subscription_id}


@celery_app.task(name="app.workers.tasks.poll_mail_intake")
def poll_mail_intake(limit: int | None = None) -> dict[str, int | str]:
    """Collect waiting notifications from the shared Outlook mailbox.

    Deliberately without a Celery retry. The intake service already retries at
    the level that matters — per message, with the attempt count in the ledger —
    and a task-level retry would re-list the whole folder to redo work that is
    already recorded as done. If the mailbox itself is unreachable, the next
    scheduled poll is the retry.
    """
    _warn_if_configuration_is_stale()

    if not settings.graph.configured:
        logger.warning("mail_intake_skipped_unconfigured")
        return {"status": "not_configured"}

    try:
        summary = run_async(run_mail_intake(limit=limit, trigger=MailIntakeTrigger.SCHEDULE))
    except Exception as exc:
        # Raised only when the mailbox could not be listed at all: a per-message
        # failure never reaches here, it becomes a ledger row.
        logger.error("mail_intake_poll_failed", error=str(exc), exc_info=exc)
        raise

    if summary.ingested:
        # Hand the new notices straight on rather than leaving them for the next
        # `process_queued_cases` tick. Collection and processing stay separate tasks
        # — an officer must still be able to trigger either alone, and a pipeline
        # failure must not read as a collection failure — but a notice that has just
        # landed should not wait out a whole beat interval before anything looks at
        # it. The beat entry remains the backstop for notices this misses: anything
        # left `queued` by a crash between the commit here and the enqueue below.
        #
        # Safe to call unconditionally. The claim is `FOR UPDATE SKIP LOCKED`, so
        # this and a concurrent beat tick divide the work instead of doubling it.
        process_queued_cases.delay()

    return {
        "status": "ok",
        "mailbox": summary.mailbox,
        "fetched": summary.fetched,
        "ingested": summary.ingested,
        "duplicates": summary.duplicates,
        "failed": summary.failed,
    }


@celery_app.task(name="app.workers.tasks.prune_mail_intake_runs")
def prune_mail_intake_runs() -> dict[str, int | str]:
    """Drop mailbox poll records older than the configured retention.

    The run ledger is what makes a stopped poller visible, and it is written once
    per poll — ten thousand rows a day at the interval this deployment uses. The
    health verdict reads only the newest row; the rest are there so somebody can
    see a pattern over the last week or two. Beyond that they are a table that
    only grows.

    Note the failure mode this task cannot have: if beat is dead, this does not
    run — but neither does the poll that writes the rows, so nothing accumulates.
    The two are silent together, which is the right way round.
    """
    if not settings.graph.configured:
        return {"status": "not_configured"}

    removed = run_async(prune_mail_intake_run_records())
    if removed:
        logger.info(
            "mail_intake_runs_pruned",
            removed=removed,
            retention_days=settings.graph.run_retention_days,
        )
    return {"status": "ok", "removed": removed}


# --- Document intelligence ----------------------------------------------------
#
# The chain that closes the seam `docs/mail-intake.md` documents. Intake leaves a
# notice at `processing_state = queued` and stops; `process_queued_cases` picks it up,
# `index_case_documents` turns its attachments into citable passages, and
# `process_case` runs the FNOL pipeline over them.


@celery_app.task(
    name="app.workers.tasks.index_document",
    bind=True,
    autoretry_for=(TransientIndexError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def index_document(self: Any, document_id: str, force: bool = False) -> dict[str, Any]:
    """Read one document into passages, and vectorise them if configured.

    Retries only `TransientIndexError` — an unreachable embedding provider or vector
    store. A document that is simply unreadable is a *permanent* outcome recorded on
    the row, and retrying it three times burns quota to reach the same conclusion.
    Classification is by exception type, never by matching words in a message: a
    document whose own text contains "timeout" must not be retried forever.
    """
    self.max_retries = max(0, settings.docint.index_max_attempts - 1)
    summary = run_async(run_document_index(uuid.UUID(document_id), force=force))
    return {
        "status": "ok",
        "document_id": document_id,
        "indexed": summary.indexed,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "reused": summary.reused,
        "chunks": summary.chunks,
        "embedded": summary.embedded,
    }


@celery_app.task(name="app.workers.tasks.index_case_documents")
def index_case_documents(
    case_id: str, force: bool = False, then_process: bool = True
) -> dict[str, Any]:
    """Index a case's documents, then run the pipeline over them.

    No Celery retry, deliberately. Each document is indexed in its own transaction and
    records its own outcome, so a retry of the whole batch would redo work that is
    already recorded as done — and the per-document attempt counter is the retry that
    actually bounds anything.
    """
    identifier = uuid.UUID(case_id)
    summary = run_async(run_case_index(identifier, force=force))

    if then_process:
        # Chained rather than called: indexing and processing have different failure
        # modes, and a pipeline failure must not look like an indexing failure.
        process_case.delay(case_id, force=force)

    return {
        "status": "ok",
        "case_id": case_id,
        "reference": summary.case_reference,
        "documents": summary.documents,
        "indexed": summary.indexed,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "reused": summary.reused,
        "chunks": summary.chunks,
        "embedded": summary.embedded,
    }


@celery_app.task(name="app.workers.tasks.process_case")
def process_case(case_id: str, force: bool = False) -> dict[str, Any]:
    """Run the FNOL pipeline over one notice.

    No retry for a stage failure: the pipeline never raises for those by design — an
    unreachable model provider or an unmatched policy becomes an exception on the
    notice, which is the state a claims officer works from.
    """
    result = run_async(run_case_pipeline(uuid.UUID(case_id), force=force))
    if result is None:
        return {"status": "not_found", "case_id": case_id}

    return {
        "status": "ok",
        "case_id": case_id,
        "reference": result.case.reference,
        "fnol_status": result.status.value,
        "exceptions_raised": result.exceptions_raised,
        "extraction_reused": result.extraction_reused,
        "documents_indexed": result.documents_indexed,
        "chunks_indexed": result.chunks_indexed,
        "retrieval_used": result.retrieval_used,
        "schema_key": result.schema_key,
        "extraction_run_id": result.extraction_run_id,
        "fields_extracted": result.fields_extracted,
        "fields_needing_review": result.fields_needing_review,
    }


@celery_app.task(name="app.workers.tasks.process_queued_cases")
def process_queued_cases(limit: int | None = None) -> dict[str, Any]:
    """Pick up notices the mailbox left queued. The beat entry.

    Claiming and enqueueing are separate steps on purpose: the claim is a short
    transaction with `FOR UPDATE SKIP LOCKED`, so two beat ticks overlapping divide the
    work rather than fighting over it or doing it twice.
    """
    if not settings.docint.enabled:
        return {"status": "disabled"}

    case_ids = run_async(claim_queued_cases(limit=limit))
    for case_id in case_ids:
        index_case_documents.delay(str(case_id))

    return {"status": "ok", "claimed": len(case_ids)}


@celery_app.task(name="app.workers.tasks.reap_stale_indexing")
def reap_stale_indexing() -> dict[str, Any]:
    """Requeue work whose worker died mid-flight.

    Without this, a worker killed between claiming a document and finishing it leaves a
    row in `indexing` that nothing ever looks at again — a document silently never
    read, on a claim someone is waiting on.
    """
    documents = run_async(release_stale_indexing())
    cases = run_async(release_stale_processing())
    return {"status": "ok", "documents_released": documents, "cases_released": cases}


# --- Policy library -----------------------------------------------------------
#
# The chain behind the Policies screen. `POST /policies/documents` stores the bytes,
# writes a row at `pending` and returns; `ingest_policy_document` reads it into
# passages and vectorises them. `ingest_pending_policy_documents` is the backstop for
# an upload whose enqueue never reached a worker — the broker being down must not mean
# a policy silently never entering the library.


@celery_app.task(
    name="app.workers.tasks.ingest_policy_document",
    bind=True,
    autoretry_for=(TransientIngestError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def ingest_policy_document(self: Any, document_id: str, force: bool = False) -> dict[str, Any]:
    """Read one policy wording into passages, and vectorise them if configured.

    Retries only `TransientIngestError` — an unreachable embedding provider or vector
    store. A wording that is simply unreadable is a *permanent* outcome recorded on the
    row, and retrying it three times burns quota to reach the same conclusion.
    """
    self.max_retries = max(0, settings.policy_library.ingest_max_attempts - 1)
    summary = run_async(run_document_ingest(uuid.UUID(document_id), force=force))
    return {
        "status": "ok",
        "document_id": document_id,
        "ingested": summary.ingested,
        "chunked_only": summary.chunked_only,
        "failed": summary.failed,
        "reused": summary.reused,
        "chunks": summary.chunks,
        "embedded": summary.embedded,
        "linked": summary.linked,
    }


@celery_app.task(name="app.workers.tasks.ingest_pending_policy_documents")
def ingest_pending_policy_documents(limit: int | None = None) -> dict[str, Any]:
    """Ingest the policy documents waiting in the queue. The beat entry.

    No Celery retry, deliberately. Each document is ingested in its own transaction
    and records its own outcome, so a retry of the whole batch would redo work that is
    already recorded as done — and the per-document attempt counter is the retry that
    actually bounds anything.
    """
    if not settings.policy_library.enabled:
        return {"status": "disabled"}

    summary = run_async(run_pending_ingest(limit=limit))
    return {
        "status": "ok",
        "documents": summary.documents,
        "ingested": summary.ingested,
        "chunked_only": summary.chunked_only,
        "failed": summary.failed,
        "reused": summary.reused,
        "chunks": summary.chunks,
        "embedded": summary.embedded,
        "linked": summary.linked,
    }


@celery_app.task(name="app.workers.tasks.reap_stale_policy_ingest")
def reap_stale_policy_ingest() -> dict[str, Any]:
    """Requeue policy ingestion whose worker died mid-flight."""
    released = run_async(release_stale_ingest())
    return {"status": "ok", "documents_released": released}
