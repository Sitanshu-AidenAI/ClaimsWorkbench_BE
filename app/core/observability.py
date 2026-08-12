"""Prometheus instrumentation.

Exposes `/metrics` (path configurable) with request counts, latency histograms
and request/response sizes.
"""

from __future__ import annotations

from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import Settings, settings

# Latency buckets tuned for an API where anything past a couple of seconds is
# already a problem worth alerting on.
LATENCY_BUCKETS: tuple[float, ...] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def setup_metrics(
    app: FastAPI,
    config: Settings | None = None,
    registry: CollectorRegistry | None = None,
) -> None:
    """Instrument `app` and expose the metrics endpoint.

    `registry` defaults to the process-global one. Pass an explicit registry to
    build more than one application in a single process — collector names are
    unique per registry, so a second app would otherwise fail to register.
    """
    config = config or settings
    if not config.observability.metrics_enabled:
        return

    # Only override the registry when one is supplied: the instrumentator's
    # default is the process-global registry, and `None` would disable
    # registration altogether.
    registry_kwargs = {"registry": registry} if registry is not None else {}

    instrumentator = Instrumentator(
        **registry_kwargs,
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        # The in-progress gauge is created in the middleware's `__init__` and
        # always lands on the global registry, ignoring the one above. That
        # makes it collide whenever the middleware stack is built more than
        # once, so it is left off; request counts and latency cover the need.
        should_instrument_requests_inprogress=False,
        excluded_handlers=[config.observability.metrics_path, "/health", "/health/ready"],
    )

    instrumentator.instrument(app, latency_lowr_buckets=LATENCY_BUCKETS).expose(
        app,
        endpoint=config.observability.metrics_path,
        include_in_schema=False,
        tags=["observability"],
    )
