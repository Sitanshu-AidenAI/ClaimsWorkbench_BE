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
