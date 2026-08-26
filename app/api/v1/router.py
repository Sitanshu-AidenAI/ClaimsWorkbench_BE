"""v1 API router.

Feature routers are registered here. Health lives under the same versioned
prefix as everything else; the unversioned liveness probe is intentionally the
only route outside it (see `app.api.v1.routes.health`).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    access,
    approvals,
    auth,
    claims,
    extraction,
    fnol,
    health,
    inspections,
    mail_intake,
    meta,
    notifications,
    policies,
)

api_router = APIRouter()

api_router.include_router(health.router)
# Registered before everything else because it is the only unauthenticated router
# besides health: nothing below it is reachable until these routes have run.
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(access.router)
api_router.include_router(fnol.router)
api_router.include_router(claims.router)
# The adjuster's board over the same records — its own prefix, so nothing here
# can shadow `claims`' `/{reference}` patterns.
api_router.include_router(inspections.router)
# The manager's queue, likewise its own prefix — see the module docstring on why
# it carries no decision endpoint of its own.
api_router.include_router(approvals.router)
api_router.include_router(mail_intake.router)
api_router.include_router(notifications.router)
# The policy library. Its own prefix, so nothing here can shadow `fnol`'s
# `/{reference}` patterns the way `extraction`'s routes could.
api_router.include_router(policies.router)
# Registered after `fnol` deliberately: it carries routes under `/fnol/...` as
# well as `/extraction/...`, and FastAPI matches in registration order, so its
# static segments must not shadow `fnol`'s `/{reference}` patterns.
api_router.include_router(extraction.router)
