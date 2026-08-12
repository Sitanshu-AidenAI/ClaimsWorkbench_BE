"""v1 API router.

Feature routers are registered here. Health lives under the same versioned
prefix as everything else; the unversioned liveness probe is intentionally the
only route outside it (see `app.api.v1.routes.health`).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import claims, fnol, health, meta

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(meta.router)
api_router.include_router(fnol.router)
api_router.include_router(claims.router)
