"""Celery application.

Worker:  celery -A app.workers.celery_app.celery_app worker -l info
Beat:    celery -A app.workers.celery_app.celery_app beat -l info

Task modules are listed in `include` so both worker and beat resolve the same
task registry.
"""

from __future__ import annotations

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
    worker_hijack_root_logger=False,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    task_always_eager=settings.celery.task_always_eager,
)

# Periodic schedule. Entries are added here as scheduled work is defined.
celery_app.conf.beat_schedule = {
    "heartbeat-every-5-minutes": {
        "task": "app.workers.tasks.heartbeat",
        "schedule": 300.0,
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
    # The backstop for a worker killed mid-index. Hourly is often enough: the failure
    # it recovers from is rare, and the recovery is not urgent so much as necessary.
    celery_app.conf.beat_schedule["reap-stale-indexing"] = {
        "task": "app.workers.tasks.reap_stale_indexing",
        "schedule": 3600.0,
    }
    logger.info(
        "document_intelligence_schedule_registered",
        interval_seconds=settings.docint.queue_poll_interval_seconds,
    )

if settings.graph.poll_enabled and settings.graph.configured:
    celery_app.conf.beat_schedule["poll-mail-intake"] = {
        "task": "app.workers.tasks.poll_mail_intake",
        "schedule": float(settings.graph.poll_interval_seconds),
    }
    logger.info(
        "mail_intake_schedule_registered",
        interval_seconds=settings.graph.poll_interval_seconds,
    )


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
