"""Metadata routes.

`/meta/config` gives the frontend the handful of values it needs to bootstrap
(OIDC endpoints, feature flags) without hardcoding them per environment.
`/meta/whoami` is the smoke test for the auth chain end to end.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps.auth import CurrentPrincipal
from app.core.config import settings

router = APIRouter(prefix="/meta", tags=["meta"])


class AuthConfig(BaseModel):
    authority: str
    client_id: str
    realm: str
    #: Whether the single sign-on button should render at all.
    #:
    #: The redirect flow needs the confidential client's secret, so a deployment
    #: without one cannot complete it. Answered here rather than assumed in the
    #: bundle, because it is a deployment fact and the whole point of this endpoint
    #: is that one build artefact is promoted through environments. A button that
    #: answers 401 is worse than no button.
    sso_enabled: bool


class ClientConfig(BaseModel):
    project_name: str
    environment: str
    version: str
    auth: AuthConfig


class WhoAmI(BaseModel):
    subject: str
    username: str | None
    email: str | None
    full_name: str | None
    roles: list[str]


@router.get("/config", response_model=ClientConfig, summary="Bootstrap config for clients")
async def client_config() -> ClientConfig:
    return ClientConfig(
        project_name=settings.project_name,
        environment=settings.environment,
        version="0.1.0",
        auth=AuthConfig(
            authority=settings.keycloak.realm_url,
            client_id=settings.keycloak.client_id,
            realm=settings.keycloak.realm,
            sso_enabled=settings.sso_available,
        ),
    )


@router.get("/whoami", response_model=WhoAmI, summary="Echo the authenticated principal")
async def whoami(principal: CurrentPrincipal) -> WhoAmI:
    return WhoAmI(
        subject=principal.subject,
        username=principal.username,
        email=principal.email,
        full_name=principal.full_name,
        roles=sorted(principal.roles),
    )
