"""Celery application.

Worker:  celery -A app.workers.celery_app.celery_app worker -l info
Beat:    celery -A app.workers.celery_app.celery_app beat -l info

Task modules are listed in `include` so both worker and beat resolve the same
task registry.
"""

from __future__ import annotations

import sys
from typing import Any

from celery import Celery
from celery.signals import setup_logging, worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

celery_app = Celery(
    "claims_workbench",
    broker=settings.redis.celery_broker_dsn,
    backend=settings.redis.celery_result_dsn,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_default_queue=settings.celery.task_default_queue,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.celery.timezone,
    enable_utc=True,
    task_track_started=True,
    task_time_limit=settings.celery.task_time_limit,
    task_soft_time_limit=settings.celery.task_soft_time_limit,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=settings.celery.worker_prefetch_multiplier,
    worker_max_tasks_per_child=settings.celery.worker_max_tasks_per_child,
    # Prefork needs fork(); Windows only has spawn, where billiard's shared
    # semaphores fail with WinError 5/6 and the pool respawns in a loop without
    # ever running a task. Solo is the only pool this codebase can use there
    # anyway: `run_async` in app/workers/tasks.py keeps one event loop per
    # *process*, so a threaded pool would raise "this event loop is already
    # running" the moment two tasks overlapped.
    #
    # `--pool` on the command line still overrides this, so the Linux containers
    # in docker-compose keep prefork at their configured concurrency.
    worker_pool="solo" if sys.platform == "win32" else "prefork",
    worker_hijack_root_logger=False,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    task_always_eager=settings.celery.task_always_eager,
)

# Periodic schedule. Entries are added here as scheduled work is defined.
#
# **Every interval below comes from `settings`, never from a literal here.** That is
# a rule rather than a style preference, and it was paid for: an interval written as
# a number in this file cannot be read out of the environment, cannot be changed
# without a code edit, and — worst of the three — cannot be *seen*. Mailbox intake
# went days without collecting because nothing anywhere stated what the scheduler
# was actually doing, and the fix for that class of fault begins with every
# schedule having exactly one place it is defined and one name it answers to.
#
# The name each one answers to is its `CWB_*` environment variable; the effective
# values are logged once at import by `_log_effective_schedule` at the bottom of
# this module, because a process reads its configuration at startup and keeps it.
celery_app.conf.beat_schedule = {
    # Named for what it is, not for how often it runs. It was
    # `heartbeat-every-5-minutes`, which stopped being true the moment the
    # interval became a setting — and a name that states a value is a second
    # place that value is written down.
    "heartbeat": {
        "task": "app.workers.tasks.heartbeat",
        "schedule": float(settings.celery.heartbeat_interval_seconds),
    },
}

# Mailbox intake is scheduled only where it is switched on *and* configured. An
# environment with no Graph credentials gets no entry at all rather than a
# schedule that fails every five minutes and buries everything else in the log.
if settings.docint.enabled and settings.docint.queue_poll_enabled:
    # The consumer of what mailbox intake leaves behind. Intake stops at
    # `processing_state = queued` by design, and before this entry existed nothing
    # read that — a collected notice sat in the queue until someone pressed a button.
    celery_app.conf.beat_schedule["process-queued-fnol-cases"] = {
        "task": "app.workers.tasks.process_queued_cases",
        "schedule": float(settings.docint.queue_poll_interval_seconds),
    }
    # The backstop for a worker killed mid-index. Hourly by default: the failure it
    # recovers from is rare, and the recovery is necessary rather than urgent.
    celery_app.conf.beat_schedule["reap-stale-indexing"] = {
        "task": "app.workers.tasks.reap_stale_indexing",
        "schedule": float(settings.docint.reap_interval_seconds),
    }
    logger.info(
        "document_intelligence_schedule_registered",
        interval_seconds=settings.docint.queue_poll_interval_seconds,
        reap_interval_seconds=settings.docint.reap_interval_seconds,
    )

# The policy library's own sweep. Registered separately from the document-intelligence
# entries above because the two are independently switchable: a deployment can run
# claim-document indexing with no policy library, and can load a policy library into an
# environment where mailbox intake is not configured at all.
if settings.policy_library.enabled:
    # The backstop for an upload whose enqueue never reached a worker.
    celery_app.conf.beat_schedule["ingest-pending-policy-documents"] = {
        "task": "app.workers.tasks.ingest_pending_policy_documents",
        "schedule": float(settings.policy_library.ingest_poll_interval_seconds),
    }
    celery_app.conf.beat_schedule["reap-stale-policy-ingest"] = {
        "task": "app.workers.tasks.reap_stale_policy_ingest",
        "schedule": float(settings.policy_library.reap_interval_seconds),
    }
    logger.info(
        "policy_library_schedule_registered",
        collection=settings.policy_library.qdrant_collection,
        interval_seconds=settings.policy_library.ingest_poll_interval_seconds,
        reap_interval_seconds=settings.policy_library.reap_interval_seconds,
    )

if settings.graph.poll_enabled and settings.graph.configured:
    celery_app.conf.beat_schedule["poll-mail-intake"] = {
        "task": "app.workers.tasks.poll_mail_intake",
        "schedule": float(settings.graph.poll_interval_seconds),
    }
    # Housekeeping on the run ledger that makes a stopped poller visible.
    celery_app.conf.beat_schedule["prune-mail-intake-runs"] = {
        "task": "app.workers.tasks.prune_mail_intake_runs",
        "schedule": float(settings.graph.run_prune_interval_seconds),
    }
    logger.info(
        "mail_intake_schedule_registered",
        interval_seconds=settings.graph.poll_interval_seconds,
        run_retention_days=settings.graph.run_retention_days,
        run_prune_interval_seconds=settings.graph.run_prune_interval_seconds,
    )


#: The environment variable each schedule answers to, for the startup log. Stated
#: here rather than derived, because the mapping from a settings field to its
#: `CWB_*` name goes through a per-class `env_prefix` and is not recoverable from
#: the value — and the whole point of the line is that a reader does not have to
#: go and work it out.
SCHEDULE_ENV_NAMES: dict[str, str] = {
    "heartbeat": "CWB_CELERY_HEARTBEAT_INTERVAL_SECONDS",
    "process-queued-fnol-cases": "CWB_DOCINT_QUEUE_POLL_INTERVAL_SECONDS",
    "reap-stale-indexing": "CWB_DOCINT_REAP_INTERVAL_SECONDS",
    "ingest-pending-policy-documents": "CWB_POLICY_INGEST_POLL_INTERVAL_SECONDS",
    "reap-stale-policy-ingest": "CWB_POLICY_REAP_INTERVAL_SECONDS",
    "poll-mail-intake": "CWB_GRAPH_POLL_INTERVAL_SECONDS",
    "prune-mail-intake-runs": "CWB_GRAPH_RUN_PRUNE_INTERVAL_SECONDS",
}


def _log_effective_schedule() -> None:
    """State, once at import, exactly what this process will run and how often.

    A worker and a beat read their configuration at import and keep it for as long
    as they live, so "what is the schedule" is a question about a *process*, not
    about a file — and an `.env` edited thirteen minutes after a worker started has
    already produced one multi-day outage on this deployment. `.env` says what the
    next process will do; this line says what this one is doing.

    It also makes an *absent* entry visible, which is the harder half. A schedule
    that was never registered because a feature switch is off looks exactly like a
    schedule that is running fine and finding nothing, and mailbox intake is the
    entry where that confusion costs claims.
    """
    entries = {
        name: float(entry["schedule"])
        for name, entry in sorted(celery_app.conf.beat_schedule.items())
        if isinstance(entry.get("schedule"), (int, float))
    }
    logger.info(
        "beat_schedule_effective",
        entries={
            name: {"every_seconds": seconds, "env": SCHEDULE_ENV_NAMES.get(name, "—")}
            for name, seconds in entries.items()
        },
        registered=len(entries),
        not_registered=sorted(set(SCHEDULE_ENV_NAMES) - set(entries)),
    )


_log_effective_schedule()


@setup_logging.connect
def _configure_celery_logging(**_kwargs: Any) -> None:
    """Use the application's structlog setup instead of Celery's own."""
    configure_logging(settings)


@worker_process_init.connect
def _on_worker_start(**_kwargs: Any) -> None:
    logger.info("celery_worker_process_started")


@worker_process_shutdown.connect
def _on_worker_stop(**_kwargs: Any) -> None:
    logger.info("celery_worker_process_stopped")
