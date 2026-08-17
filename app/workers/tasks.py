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

from app.core.config import settings
from app.core.logging import get_logger
from app.db.pool import close_pool, init_pool
from app.db.session import dispose_engine, init_engine
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
from app.services.mail.runner import run_mail_intake
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


@celery_app.task(name="app.workers.tasks.poll_mail_intake")
def poll_mail_intake(limit: int | None = None) -> dict[str, int | str]:
    """Collect waiting notifications from the shared Outlook mailbox.

    Deliberately without a Celery retry. The intake service already retries at
    the level that matters — per message, with the attempt count in the ledger —
    and a task-level retry would re-list the whole folder to redo work that is
    already recorded as done. If the mailbox itself is unreachable, the next
    scheduled poll is the retry.
    """
    if not settings.graph.configured:
        logger.warning("mail_intake_skipped_unconfigured")
        return {"status": "not_configured"}

    try:
        summary = run_async(run_mail_intake(limit=limit))
    except Exception as exc:
        # Raised only when the mailbox could not be listed at all: a per-message
        # failure never reaches here, it becomes a ledger row.
        logger.error("mail_intake_poll_failed", error=str(exc), exc_info=exc)
        raise

    return {
        "status": "ok",
        "mailbox": summary.mailbox,
        "fetched": summary.fetched,
        "ingested": summary.ingested,
        "duplicates": summary.duplicates,
        "failed": summary.failed,
    }


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
