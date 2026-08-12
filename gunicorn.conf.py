"""Gunicorn configuration for production.

Uvicorn workers under Gunicorn:

    gunicorn main:app -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py

Worker count defaults to `2 * cpu + 1`, capped — each worker holds its own
asyncpg pool and SQLAlchemy engine, so worker count multiplies the connection
footprint against Postgres.
"""

from __future__ import annotations

import multiprocessing
import os

_cpu_count = multiprocessing.cpu_count()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.isdigit() else default


bind = f"{os.getenv('CWB_HOST', '0.0.0.0')}:{os.getenv('CWB_PORT', '8000')}"

worker_class = "uvicorn.workers.UvicornWorker"
workers = _env_int("CWB_WEB_CONCURRENCY", min(2 * _cpu_count + 1, 8))

# Long enough for slow upstreams, short enough that a wedged worker is recycled.
timeout = _env_int("CWB_WORKER_TIMEOUT", 60)
graceful_timeout = _env_int("CWB_WORKER_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("CWB_WORKER_KEEPALIVE", 5)

# Recycle workers periodically to bound the effect of any slow leak. The jitter
# keeps every worker from restarting at once.
max_requests = _env_int("CWB_MAX_REQUESTS", 2000)
max_requests_jitter = _env_int("CWB_MAX_REQUESTS_JITTER", 200)

preload_app = False  # each worker builds its own event loop and pools

# structlog owns formatting; Gunicorn just needs to emit to stdout/stderr.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("CWB_OBS_LOG_LEVEL", "info").lower()
