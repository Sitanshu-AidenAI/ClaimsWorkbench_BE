"""Liveness and readiness probes.

Liveness answers "is the process up"; readiness actually touches Postgres and
Redis, so an orchestrator does not route traffic to a container whose
dependencies are still coming up.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger
from app.db import pool
from app.services.cache import ping_redis

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    environment: str
    version: str


class DependencyStatus(BaseModel):
    postgres: bool
    redis: bool


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    dependencies: DependencyStatus


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    return HealthResponse(environment=settings.environment, version="0.1.0")


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(response: Response) -> ReadinessResponse:
    postgres_ok = await _check_postgres()
    redis_ok = await ping_redis()

    ready = postgres_ok and redis_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "degraded",
        dependencies=DependencyStatus(postgres=postgres_ok, redis=redis_ok),
    )


async def _check_postgres() -> bool:
    try:
        return await pool.fetchval("SELECT 1") == 1
    except Exception as exc:
        logger.warning("readiness_postgres_failed", error=str(exc))
        return False
