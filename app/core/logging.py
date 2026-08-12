"""structlog configuration.

Console renderer locally, JSON in staging/production. The stdlib logging root is
routed through structlog so third-party loggers (uvicorn, sqlalchemy, celery)
emit in the same format.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.config import Settings, settings
from app.core.context import get_request_id


def _add_request_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach the ambient request id, when one is bound to this context."""
    request_id = get_request_id()
    if request_id:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def configure_logging(config: Settings | None = None) -> None:
    config = config or settings
    level = logging.getLevelNamesMapping().get(config.observability.log_level.upper(), logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_request_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.typing.Processor
    if config.log_as_json:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn ships its own handlers; drop them so records reach the root handler once.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn", "gunicorn.error"):
        third_party = logging.getLogger(name)
        third_party.handlers = []
        third_party.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if config.postgres.sqlalchemy_echo else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
